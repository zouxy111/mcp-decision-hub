import httpx
import pytest

from hub.llm.client import (
    LLM_AUTH_FAILED,
    LLM_HTTP_ERROR,
    LLM_INVALID_JSON,
    LLM_NOT_CONFIGURED,
    LLM_SCHEMA_INVALID,
    LLM_TIMEOUT,
    DeepSeekClient,
    LLMError,
)

SUMMARY_OK = {
    "consensus_points": ["共识"],
    "divergences": [],
    "blind_spots": [],
    "open_questions": ["问题"],
    "convergence": "continue",
}


def _response(payload, status=200):
    import json

    body = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    return httpx.Response(status, json=body)


def _raw_response(text, status=200):
    return httpx.Response(status, text=text)


def _make_client(handler, **overrides):
    transport = httpx.MockTransport(handler)
    kwargs = dict(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=120,
        http_client=httpx.Client(transport=transport),
        sleep_fn=lambda _s: None,
    )
    kwargs.update(overrides)
    return DeepSeekClient(**kwargs)


def test_success_no_retry():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer sk-test"
        return _response(SUMMARY_OK)

    client = _make_client(handler)
    result = client.complete_json("sys", "user", schema_name="round_summary")
    assert result == SUMMARY_OK
    assert len(calls) == 1


def test_invalid_json_retried_then_success():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return _raw_response('{"choices": []}')  # unparseable envelope
        return _response(SUMMARY_OK)

    client = _make_client(handler)
    assert client.complete_json("s", "u", schema_name="round_summary") == SUMMARY_OK
    assert len(calls) == 3


def test_timeout_retried_then_success():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 2:
            raise httpx.ReadTimeout("boom")
        return _response(SUMMARY_OK)

    client = _make_client(handler)
    assert client.complete_json("s", "u", schema_name="round_summary") == SUMMARY_OK
    assert len(calls) == 2


def test_http_401_fails_immediately_without_retry():
    """【断言翻转 · C2 修复】401 属 4xx 客户端错误，立即失败不重试
    （r5Am9i R1；旧断言「重试 3 次耗尽」编码的正是被修掉的缺陷）。"""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_AUTH_FAILED
    assert exc.value.retry_count == 0
    assert len(calls) == 1  # 只有首次，零重试


def test_invalid_json_exhausted_raises_with_retry_count():
    calls = []

    def handler(request):
        calls.append(request)
        return _raw_response("not json at all")

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_INVALID_JSON
    assert exc.value.retry_count == 3
    assert len(calls) == 4


def test_http_500_maps_to_http_error():
    def handler(request):
        return httpx.Response(500, text="server error")

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_HTTP_ERROR
    assert exc.value.retry_count == 3


def test_timeout_exhausted():
    def handler(request):
        raise httpx.ReadTimeout("boom")

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_TIMEOUT
    assert exc.value.retry_count == 3


def test_missing_api_key_fails_without_retry():
    client = _make_client(lambda request: httpx.Response(200), api_key=None)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_NOT_CONFIGURED
    assert exc.value.retry_count == 0


def test_illegal_convergence_is_schema_error_and_retried():
    bad = dict(SUMMARY_OK, convergence="done")
    calls = []

    def handler(request):
        calls.append(request)
        return _response(bad)

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_SCHEMA_INVALID
    assert exc.value.retry_count == 3
    assert len(calls) == 4


def test_empty_summary_rejected():
    empty = {
        "consensus_points": [], "divergences": [],
        "blind_spots": [], "open_questions": [],
        "convergence": "converged",
    }

    def handler(request):
        return _response(empty)

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="round_summary")
    assert exc.value.error_code == LLM_SCHEMA_INVALID


def test_questions_schema():
    def handler(request):
        return _response({"questions": ["Q1?", "Q2?"]})

    client = _make_client(handler)
    assert client.complete_json("s", "u", schema_name="questions") == {
        "questions": ["Q1?", "Q2?"]
    }


def test_questions_schema_rejects_empty_questions():
    def handler(request):
        return _response({"questions": ["  ", ""]})

    client = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("s", "u", schema_name="questions")
    assert exc.value.error_code == LLM_SCHEMA_INVALID


def test_backoff_schedule():
    """【断言翻转 · r5Am9i R8】退避带乘性 jitter：以默认随机源注入时序列不再是
    恰好 [1, 2, 4]，但仍单调递增（在 base=1、±20% 抖动下数学上不可能逆序），
    且次数不变。确定性序列断言移至 test_client_retry.py::test_R8（固定种子）。"""
    sleeps = []

    def handler(request):
        return httpx.Response(500)

    client = _make_client(handler, sleep_fn=sleeps.append)
    with pytest.raises(LLMError):
        client.complete_json("s", "u", schema_name="round_summary")
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps)
    assert all(0 < d <= 30.0 for d in sleeps)

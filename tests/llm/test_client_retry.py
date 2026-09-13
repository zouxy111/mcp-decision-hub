"""r5Am9i 分级重试 · R1–R10 验收（C1 固定种子 jitter / C4 次数硬指标）。

C2：llm/client.py 的「401 也重试」缺陷在此单独修复与测试，不混在 MCP 改动里。
"""

import random
import time

import httpx
import pytest

from hub.domain.retry import backoff_delays, should_retry
from hub.llm.client import (
    LLM_AUTH_FAILED,
    LLM_HTTP_ERROR,
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


def _make_client(handler, *, recorded_delays=None, **overrides):
    seen = []

    def counting_sleep(seconds):
        seen.append(seconds)
        if recorded_delays is not None:
            recorded_delays.append(seconds)

    transport = httpx.MockTransport(handler)
    kwargs = dict(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        timeout_seconds=120,
        http_client=httpx.Client(transport=transport),
        sleep_fn=counting_sleep,
    )
    kwargs.update(overrides)
    return DeepSeekClient(**kwargs), seen


def _ok():
    import json

    body = {"choices": [{"message": {"content": json.dumps(SUMMARY_OK)}}]}
    return httpx.Response(200, json=body)


def test_R1_4xx不重试_401只尝试一次():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    client, delays = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 1          # C2 修复点：401 不再白等 3 次
    assert delays == []
    assert exc.value.error_code == LLM_AUTH_FAILED
    assert exc.value.status_code == 401


def test_R1_其他4xx同样不重试_400():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "bad request"})

    client, delays = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 1
    assert exc.value.status_code == 400


def test_R2_429重试后成功():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429) if len(calls) == 1 else _ok()

    client, delays = _make_client(handler)
    assert client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 2
    assert len(delays) == 1


def test_R3_5xx重试后成功():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) < 3 else _ok()

    client, _ = _make_client(handler)
    assert client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 3


def test_R4_网络错误重试():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return _ok()

    client, delays = _make_client(handler)
    assert client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 2 and len(delays) == 1


def test_R5_超时重试():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return _ok()

    client, delays = _make_client(handler)
    assert client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 2 and len(delays) == 1


def test_R6_全程失败总尝试次数为上限加一且错误带分类():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    client, _ = _make_client(handler)
    with pytest.raises(LLMError) as exc:
        client.complete_json("sys", "user", schema_name="round_summary")
    assert len(calls) == 4          # MAX_RETRIES(3) + 1 —— 次数是硬指标
    assert exc.value.error_code == LLM_HTTP_ERROR
    assert exc.value.retry_count == 3


def test_R7_退避单调递增且每次不超过防御性上限():
    recorded = []

    def handler(request):
        return httpx.Response(500)

    client, _ = _make_client(handler, recorded_delays=recorded, rng=random.Random(7))
    with pytest.raises(LLMError):
        client.complete_json("sys", "user", schema_name="round_summary")
    assert recorded == sorted(recorded)          # 单调递增
    assert all(d <= 30.0 for d in recorded)      # ≤ 上限（C4：不要求触到）


def test_R8_jitter_同种子可复现_异种子序列不同():
    a = backoff_delays(retries=3, base=1.0, cap=30.0, rng=random.Random(42))
    b = backoff_delays(retries=3, base=1.0, cap=30.0, rng=random.Random(42))
    c = backoff_delays(retries=3, base=1.0, cap=30.0, rng=random.Random(43))
    assert a == b                                # C1：固定种子 → 可复现
    assert a != c                                # 不同种子 → 序列不同
    assert all(0 < d <= 30.0 for d in a)         # 抖动不得越界


def test_R9_假sleep注入_测试不真等():
    started = time.monotonic()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    client, _ = _make_client(handler)
    with pytest.raises(LLMError):
        client.complete_json("sys", "user", schema_name="round_summary")
    assert time.monotonic() - started < 1.0
    assert len(calls) == 4


def test_R10_错误分类可辨识_4xx与重试耗尽不同():
    def always_401(request):
        return httpx.Response(401)

    client, _ = _make_client(always_401)
    with pytest.raises(LLMError) as client_error:
        client.complete_json("sys", "user", schema_name="round_summary")
    assert client_error.value.error_code == LLM_AUTH_FAILED
    assert client_error.value.retry_count == 0   # 未消耗任何重试

    def always_500(request):
        return httpx.Response(500)

    client, _ = _make_client(always_500)
    with pytest.raises(LLMError) as exhausted:
        client.complete_json("sys", "user", schema_name="round_summary")
    assert exhausted.value.error_code == LLM_HTTP_ERROR
    assert exhausted.value.retry_count == 3      # 重试耗尽
    assert exhausted.value.error_code != client_error.value.error_code


def test_should_retry_纯函数契约():
    assert should_retry(error_code="LLM_TIMEOUT") is True
    assert should_retry(error_code="LLM_NETWORK_ERROR") is True
    assert should_retry(error_code="LLM_INVALID_JSON") is True
    assert should_retry(error_code="LLM_SCHEMA_INVALID") is True
    assert should_retry(error_code="LLM_HTTP_ERROR", status_code=429) is True
    assert should_retry(error_code="LLM_HTTP_ERROR", status_code=500) is True
    assert should_retry(error_code="LLM_HTTP_ERROR", status_code=400) is False
    assert should_retry(error_code="LLM_AUTH_FAILED", status_code=401) is False
    assert should_retry(error_code="LLM_NOT_CONFIGURED") is False

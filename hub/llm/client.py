"""DeepSeek client (OpenAI-compatible chat completions), sync httpx.

Retry policy (r5Am9i 分级重试，R1–R10；C2 修正旧「401 也重试」缺陷):
429 / 5xx / network errors / timeouts / invalid JSON / schema violations are
retried up to MAX_RETRIES times with exponential backoff + jitter (capped);
4xx client errors — including 401/403 auth failures — fail immediately
without retry. A missing API key fails immediately without retry.
Classification and backoff live in :mod:`hub.domain.retry` (pure domain).

Logging discipline (PRD 10.1): only schema_name, duration, error code and
retry count are logged — never prompts, completions or API keys.
"""

import json
import logging
import random
import time
from collections.abc import Callable

import httpx

from hub.domain import retry as retry_policy
from hub.domain.convergence import ConvergenceValidationError, validate_convergence

logger = logging.getLogger(__name__)

LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
LLM_INVALID_JSON = "LLM_INVALID_JSON"
LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
LLM_TIMEOUT = "LLM_TIMEOUT"
LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
LLM_HTTP_ERROR = "LLM_HTTP_ERROR"
LLM_NETWORK_ERROR = "LLM_NETWORK_ERROR"
# 模型 id 不存在（上游 400 / Model Not Exist）。与通用 HTTP 错误分开，是因为
# 它有一个确定的、与 key 无关的处置办法：把模型名改对。
LLM_MODEL_NOT_FOUND = "LLM_MODEL_NOT_FOUND"

# 上游错误正文回填进消息时截断到多长。上游 400 的正文是 JSON，也可能是一大坨
# HTML（反代页面）——原样塞进消息会污染页面与日志。
UPSTREAM_BODY_MAX = 200

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0


class LLMError(Exception):
    def __init__(self, error_code: str, message: str, *, retry_count: int,
                 status_code: int | None = None):
        self.error_code = error_code
        self.retry_count = retry_count
        self.status_code = status_code
        super().__init__(message)


class LLMSchemaError(ValueError):
    """LLM output does not match the requested JSON contract."""


def _require_str_list(data: dict, key: str) -> None:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
        raise LLMSchemaError(f"{key} 必须是字符串数组")


def _upstream_message(resp: httpx.Response) -> str:
    """从上游错误响应里取一句人话，取不到就退回**截断后**的原始正文。

    上游 400 的正文形如 ``{"error": {"message": "Model Not Exist"}}``；反代
    故障时正文可能是整页 HTML。所以一律截断，绝不原样回填到消息里。
    """
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            return message.strip()[:UPSTREAM_BODY_MAX]
    text = " ".join((resp.text or "").split())
    return text[:UPSTREAM_BODY_MAX] or "（无正文）"


def _validate_schema(schema_name: str, data) -> None:
    if not isinstance(data, dict):
        raise LLMSchemaError("顶层必须是 JSON 对象")
    if schema_name == "round_summary":
        for key in ("consensus_points", "divergences", "blind_spots", "open_questions"):
            _require_str_list(data, key)
        substantive = ("consensus_points", "divergences", "open_questions")
        if not any(data[key] for key in substantive):
            raise LLMSchemaError("摘要四块不得全部为空（FR-18 不生成空摘要）")
        validate_convergence(data.get("convergence"))
    elif schema_name == "questions":
        _require_str_list(data, "questions")
        if not data["questions"] or any(not q.strip() for q in data["questions"]):
            raise LLMSchemaError("问题列表为空或含空问题（FR-18 不生成空问题）")
    elif schema_name == "resolution_draft":
        if not isinstance(data.get("recommendation"), str) or not data[
            "recommendation"
        ].strip():
            raise LLMSchemaError("recommendation 必须是非空字符串（FR-18 不生成空决议）")
        if not isinstance(data.get("rationale"), str) or not data["rationale"].strip():
            raise LLMSchemaError("rationale 必须是非空字符串")
        _require_str_list(data, "risks")
        _require_str_list(data, "divergences")
        cited = data.get("cited_rounds")
        if (
            not isinstance(cited, list)
            or not cited
            or any(not isinstance(n, int) or isinstance(n, bool) for n in cited)
        ):
            raise LLMSchemaError("cited_rounds 必须是非空整数数组")
    else:
        raise LLMSchemaError(f"未知 schema: {schema_name}")


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int = MAX_RETRIES,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._http = http_client or httpx.Client(timeout=timeout_seconds)
        self._sleep = sleep_fn
        self._rng = rng

    def complete_json(
        self, system_prompt: str, user_prompt: str, *, schema_name: str
    ) -> dict:
        if not self._api_key:
            raise LLMError(
                LLM_NOT_CONFIGURED,
                "未配置 DEEPSEEK_API_KEY，无法调用 LLM",
                retry_count=0,
            )
        started = time.monotonic()
        last_error: LLMError | None = None
        delays = retry_policy.backoff_delays(
            retries=self._max_retries,
            base=self._backoff_base,
            rng=self._rng,
        )
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                self._sleep(delays[attempt - 1])
            try:
                result = self._call_once(system_prompt, user_prompt,
                                         schema_name=schema_name)
                logger.info(
                    "llm_call ok type=%s duration=%.2fs retries=%d",
                    schema_name, time.monotonic() - started, attempt,
                )
                return result
            except LLMError as e:
                if not retry_policy.should_retry(
                    error_code=e.error_code, status_code=e.status_code
                ):
                    # R1：4xx 客户端错误（认证失败等）立即失败，不消耗重试。
                    raise
                last_error = e
                logger.warning(
                    "llm_call failed type=%s error_code=%s retry=%d",
                    schema_name, e.error_code, attempt,
                )
        raise LLMError(
            last_error.error_code, str(last_error), retry_count=self._max_retries,
            status_code=last_error.status_code,
        ) from last_error

    def probe(self) -> float:
        """最小连通性探测：真发一条请求，成功返回耗时（秒）。

        与 :meth:`complete_json` 的区别：不要 JSON、不做 schema 校验、不重试。
        它回答的是「这把 key + 这个 model id 现在能不能用」，不是业务链路能跑。
        所以**必须真发请求** —— 本地校验一个 key 的格式永远无法证明它有效。

        单次而非重试：这是给人在页面上按的按钮，重试只会把「key 错了」这件事
        拖成几十秒的等待，而不是让它更快被看见。
        """
        if not self._api_key:
            raise LLMError(LLM_NOT_CONFIGURED, "未配置 API Key，无法调用 LLM",
                           retry_count=0)
        started = time.monotonic()
        try:
            resp = self._http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
        except httpx.TimeoutException as e:
            raise LLMError(LLM_TIMEOUT,
                           f"LLM 请求超时（{self._timeout}s）", retry_count=0) from e
        except httpx.HTTPError as e:
            raise LLMError(LLM_NETWORK_ERROR,
                           f"LLM 网络错误: {type(e).__name__}", retry_count=0) from e
        if resp.status_code in (401, 403):
            raise LLMError(LLM_AUTH_FAILED,
                           f"API Key 被拒绝（HTTP {resp.status_code}）",
                           retry_count=0, status_code=resp.status_code)
        if resp.status_code != 200:
            upstream = _upstream_message(resp)
            if resp.status_code == 400 and "model" in upstream.lower():
                raise LLMError(
                    LLM_MODEL_NOT_FOUND,
                    f"模型「{self._model}」不可用：{upstream}",
                    retry_count=0, status_code=resp.status_code)
            raise LLMError(LLM_HTTP_ERROR,
                           f"上游返回 HTTP {resp.status_code}：{upstream}",
                           retry_count=0, status_code=resp.status_code)
        return time.monotonic() - started

    def _call_once(self, system_prompt: str, user_prompt: str, *,
                   schema_name: str) -> dict:
        try:
            resp = self._http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
            )
        except httpx.TimeoutException as e:
            raise LLMError(LLM_TIMEOUT,
                           f"LLM 请求超时（{self._timeout}s）", retry_count=0) from e
        except httpx.HTTPError as e:
            raise LLMError(LLM_NETWORK_ERROR,
                           f"LLM 网络错误: {type(e).__name__}", retry_count=0) from e
        if resp.status_code in (401, 403):
            raise LLMError(LLM_AUTH_FAILED,
                           f"LLM 认证失败（HTTP {resp.status_code}）",
                           retry_count=0, status_code=resp.status_code)
        if resp.status_code != 200:
            raise LLMError(LLM_HTTP_ERROR,
                           f"LLM HTTP 错误（{resp.status_code}）",
                           retry_count=0, status_code=resp.status_code)
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMError(LLM_INVALID_JSON, "LLM 返回无效 JSON", retry_count=0) from e
        try:
            _validate_schema(schema_name, data)
        except (LLMSchemaError, ConvergenceValidationError) as e:
            raise LLMError(LLM_SCHEMA_INVALID,
                           f"LLM 返回结构不符合契约: {e}", retry_count=0) from e
        return data

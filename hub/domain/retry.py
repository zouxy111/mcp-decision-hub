"""分级重试退避策略（r5Am9i · 纯函数、零 IO，符合项目「纯函数域」惯例）。

分级（R1–R5）：4xx 客户端错误（认证失败等）**不重试**；429 / 5xx / 网络错误 /
超时 / 无效 JSON / schema 不符**重试**。
退避（R7 / C4）：指数 ``base * 2 ** (attempt - 1)``；上限 cap 只是**防御性封顶**
（C4 定稿：重试次数才是硬指标，退避不要求触到上限）。
jitter（R8 / C1）：乘性抖动由**注入的随机源**产生 —— 注入固定种子的
``random.Random(k)`` 即可复现（不 flaky），不同种子产生不同序列。

契约：``should_retry`` / ``backoff_delays`` 只吃原始类型（错误码字符串、状态码
整数、随机源），不导入任何上层模块 —— 错误码字符串的取值契约由调用方（如
``hub.llm.client``）持有。
"""

from __future__ import annotations

import random

# 认证/配置类错误重试无意义且有害（为 401 白等 3 次是缺陷）。
CLIENT_ERROR_CODES = frozenset({"LLM_NOT_CONFIGURED", "LLM_AUTH_FAILED"})
# 与传输/模型输出相关、重试有可能成功的错误。
RETRYABLE_CODES = frozenset({
    "LLM_TIMEOUT",
    "LLM_NETWORK_ERROR",
    "LLM_INVALID_JSON",
    "LLM_SCHEMA_INVALID",
})
# HTTP 状态码错误单独分级：429 与 5xx 重试，其余 4xx 不重试。
HTTP_CODE = "LLM_HTTP_ERROR"

JITTER_LOW = 0.8
JITTER_HIGH = 1.2
DEFAULT_CAP_SECONDS = 30.0


def should_retry(*, error_code: str, status_code: int | None = None) -> bool:
    """按事项原文分级：只重试网络 / 429 / 5xx / 超时；不重试 4xx。"""
    if error_code in CLIENT_ERROR_CODES:
        return False
    if error_code == HTTP_CODE:
        if status_code is None:
            return True  # 无状态码信息的 HTTP 错误按可重试处理（保守）
        return status_code == 429 or status_code >= 500
    return error_code in RETRYABLE_CODES


def backoff_delays(
    *,
    retries: int,
    base: float,
    cap: float = DEFAULT_CAP_SECONDS,
    rng: random.Random | None = None,
) -> list[float]:
    """每次重试前的等待时长序列（长度 == retries）。

    指数 ``base * 2 ** (attempt - 1)``，经乘性抖动（[JITTER_LOW, JITTER_HIGH)）
    后按 cap 封顶。``rng`` 注入：固定种子即可复现（C1）。
    """
    source = rng if rng is not None else random.Random()
    delays: list[float] = []
    for attempt in range(1, retries + 1):
        raw = base * 2 ** (attempt - 1)
        jittered = raw * source.uniform(JITTER_LOW, JITTER_HIGH)
        delays.append(min(jittered, cap))
    return delays

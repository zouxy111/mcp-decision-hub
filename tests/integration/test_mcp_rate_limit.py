"""MCP rate limiting tests (PRD 9.1, M4 任务 4).

Domain 层单测覆盖滑动窗口语义。集成层的 token/account 限流通过
``test_mcp_integration.py`` 的真实 uvicorn + fastmcp.Client 路径间接覆盖
（大阈值下不触发限流即正常行为），小阈值触发 429 的场景需要独立 uvicorn
进程（与 test_mcp_integration.py 同模式），留待任务 11 手工冒烟验证。
"""


from hub.domain.rate_limit import RateLimiter


def test_first_n_requests_allowed():
    rl = RateLimiter(window_seconds=60)
    now = 1000.0
    for _ in range(5):
        ok, retry = rl.allow("k", limit=5, now=now)
        assert ok is True
        assert retry == 0


def test_nth_plus_one_request_rejected():
    rl = RateLimiter(window_seconds=60)
    now = 1000.0
    for _ in range(5):
        rl.allow("k", limit=5, now=now)
    ok, retry = rl.allow("k", limit=5, now=now)
    assert ok is False
    assert retry >= 1


def test_different_keys_independent():
    rl = RateLimiter(window_seconds=60)
    now = 1000.0
    for _ in range(5):
        rl.allow("a", limit=5, now=now)
    ok, _ = rl.allow("b", limit=5, now=now)
    assert ok is True


def test_window_slides():
    rl = RateLimiter(window_seconds=60)
    t0 = 1000.0
    for _ in range(5):
        rl.allow("k", limit=5, now=t0)
    # at t0 + 59s: still within window → rejected
    ok, _ = rl.allow("k", limit=5, now=t0 + 59)
    assert ok is False
    # at t0 + 61s: oldest entry slid out → allowed
    ok, retry = rl.allow("k", limit=5, now=t0 + 61)
    assert ok is True
    assert retry == 0


def test_retry_after_is_correct():
    rl = RateLimiter(window_seconds=60)
    t0 = 1000.0
    for _ in range(3):
        rl.allow("k", limit=3, now=t0)
    ok, retry = rl.allow("k", limit=3, now=t0 + 10)
    assert ok is False
    # oldest hit at t0, window=60, now=t0+10 → retry after (t0+60)-(t0+10) = 50s
    assert retry == 50


def test_retry_after_minimum_one():
    rl = RateLimiter(window_seconds=60)
    t0 = 1000.0
    for _ in range(3):
        rl.allow("k", limit=3, now=t0)
    ok, retry = rl.allow("k", limit=3, now=t0 + 59.9)
    assert ok is False
    assert retry >= 1


def test_keys_from_helpers():
    """Verify the key helper functions produce correct key formats."""
    from hub.domain.rate_limit import (
        rate_limit_key_account,
        rate_limit_key_submit,
        rate_limit_key_token,
    )
    assert rate_limit_key_token("tok_abc") == "token:tok_abc"
    assert rate_limit_key_submit("tok_abc") == "token:tok_abc:submit"
    assert rate_limit_key_account(42) == "account:42"

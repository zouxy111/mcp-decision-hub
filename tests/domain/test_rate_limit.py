"""Rate limiter unit tests (PRD 9.1, M4 任务 4)."""


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
    assert retry >= 1  # at least 1 second


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
    # now very close to the window edge
    ok, retry = rl.allow("k", limit=3, now=t0 + 59.9)
    assert ok is False
    assert retry >= 1  # ceil should be at least 1

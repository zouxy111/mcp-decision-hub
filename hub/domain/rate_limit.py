"""MCP rate limiter — sliding window per key (PRD 9.1).

Pure in-memory, single-process deployment. Thresholds are configurable
via Settings. Keys: ``token:{id}``, ``token:{id}:submit``, ``account:{user_id}``.
Thread-safe (``threading.Lock``). Window = 60s.
"""

import math
import threading
import time
from collections import deque


class RateLimiter:
    """Sliding window rate limiter. Each key tracks request timestamps within
    a fixed ``window_seconds`` window.

    Returns ``(allowed: bool, retry_after: int)`` where ``retry_after`` is the
    number of seconds until the oldest entry leaves the window (ceiling)."""

    def __init__(self, *, window_seconds: int = 60):
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(
        self, key: str, *, limit: int, now: float | None = None,
    ) -> tuple[bool, int]:
        """Check if a request is allowed under the given key and limit.

        Returns ``(allowed, retry_after)``. If ``allowed`` is ``False``,
        ``retry_after`` is the number of seconds the caller should wait
        before retrying (ceiling).
        """
        if now is None:
            now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque()
                self._hits[key] = q
            # purge stale entries
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) < limit:
                q.append(now)
                return True, 0
            # rejected: retry after the oldest entry leaves the window
            retry_after = math.ceil(q[0] + self._window - now)
            return False, max(retry_after, 1)


def rate_limit_key_token(token_id: str) -> str:
    return f"token:{token_id}"


def rate_limit_key_submit(token_id: str) -> str:
    return f"token:{token_id}:submit"


def rate_limit_key_account(user_id: int) -> str:
    return f"account:{user_id}"

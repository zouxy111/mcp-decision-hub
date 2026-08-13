"""Bearer token auth + rate limiting middleware for the MCP endpoint.

Fail closed with 401 for auth; fail closed with 429 for rate limits.
PRD 9.1: token/account dual-counting with Retry-After header. The
submit-specific limit is enforced at the tool layer (hub/mcp_server/tools.py)
to avoid ASGI body-buffering complexity — same error code, different transport.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from hub.api.tokens import find_user_by_token
from hub.domain.rate_limit import (
    RateLimiter,
    rate_limit_key_account,
    rate_limit_key_token,
)


def _retry_after_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"error_code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试"},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


class BearerAuthMiddleware:
    def __init__(self, app, session_factory, settings=None, limiter=None):
        self.app = app
        self.session_factory = session_factory
        self.settings = settings
        self.limiter: RateLimiter | None = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        header = request.headers.get("authorization", "")
        plaintext = None
        if header.lower().startswith("bearer "):
            plaintext = header[7:].strip()
        user = None
        token_id = None
        if plaintext:
            with self.session_factory() as session:
                token_obj = find_user_by_token(session, plaintext)
                if token_obj is not None:
                    user = token_obj
                    token_id = token_obj.id
                session.commit()  # persist last_used_at
        if user is None:
            response = JSONResponse(
                {"error_code": "AUTH_INVALID_TOKEN",
                 "message": "Token 缺失、无效或已吊销"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["user_id"] = user.id
        scope.setdefault("state", {})["token_id"] = token_id

        # --- rate limiting (PRD 9.1): token + account dimensions ---
        if self.limiter is not None and self.settings is not None:
            settings = self.settings
            # Token-wide limit
            tok_key = rate_limit_key_token(token_id)
            ok, retry = self.limiter.allow(
                tok_key, limit=settings.rate_limit_token_per_minute)
            if not ok:
                resp = _retry_after_response(retry)
                await resp(scope, receive, send)
                return
            # Account-wide limit (multi-token total)
            acct_key = rate_limit_key_account(user.id)
            ok, retry = self.limiter.allow(
                acct_key, limit=settings.rate_limit_account_per_minute)
            if not ok:
                resp = _retry_after_response(retry)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)

"""Bearer token auth middleware for the MCP endpoint. Fail closed with 401."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from hub.api.tokens import find_user_by_token


class BearerAuthMiddleware:
    def __init__(self, app, session_factory):
        self.app = app
        self.session_factory = session_factory

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
        if plaintext:
            with self.session_factory() as session:
                user = find_user_by_token(session, plaintext)
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
        await self.app(scope, receive, send)

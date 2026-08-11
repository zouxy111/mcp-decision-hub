"""FastMCP tool shells: extract identity, call methods, translate errors.

Tools are added by tasks 19-21; this task only provides the shared plumbing.
"""

import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request

from hub.api.errors import ApiError, error_payload
from hub.config import Settings


def _current_user_id() -> int:
    request = get_http_request()
    return request.state.user_id


def _call(session_factory, fn, **kwargs):
    """Run a method with a session; commit on success; translate ApiError to
    ToolError carrying the {error_code, message, details?} JSON payload."""
    with session_factory() as session:
        try:
            result = fn(session, **kwargs)
        except ApiError as e:
            session.commit()  # persist audit rows written before the error
            raise ToolError(json.dumps(error_payload(e), ensure_ascii=False)) from e
        session.commit()
        return result


def register_tools(mcp: FastMCP, session_factory, settings: Settings) -> None:
    from hub.mcp_server import methods

    @mcp.tool
    def list_pending_tasks(limit: int = 20, cursor: str | None = None) -> dict:
        """List pending tasks owned by the caller's token user."""
        return _call(
            session_factory, methods.mcp_list_pending_tasks,
            settings=settings, user_id=_current_user_id(), limit=limit,
            cursor=cursor,
        )

    @mcp.tool
    def get_task(task_id: str) -> dict:
        """Get a task with its context package (no other participants' answers)."""
        return _call(
            session_factory, methods.mcp_get_task,
            settings=settings, user_id=_current_user_id(), task_id=task_id,
        )

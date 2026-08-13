"""MCP sub-application assembly."""

from fastmcp import FastMCP

from hub.config import Settings
from hub.mcp_server.auth import BearerAuthMiddleware
from hub.mcp_server.tools import register_tools


def create_mcp_asgi(session_factory, settings: Settings, drive_queue=None,
                    limiter=None):
    """Returns (mounted_app, inner_lifespan). mounted_app is wrapped with the
    bearer auth middleware; inner_lifespan must be entered by the parent app so
    the MCP session manager starts. drive_queue (optional) receives round_ids
    to drive after successful submissions. limiter (optional) enables PRD 9.1
    rate limiting."""
    mcp = FastMCP("mcp-decision-hub")
    register_tools(mcp, session_factory, settings, drive_queue, limiter=limiter)
    inner = mcp.http_app(path="/")
    inner_lifespan = getattr(inner, "lifespan", None) or inner.router.lifespan_context
    return BearerAuthMiddleware(inner, session_factory, settings, limiter), inner_lifespan

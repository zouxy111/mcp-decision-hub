"""MCP sub-application assembly."""

from fastmcp import FastMCP

from hub.config import Settings
from hub.mcp_server.auth import BearerAuthMiddleware
from hub.mcp_server.tools import register_tools


def create_mcp_asgi(session_factory, settings: Settings, drive_queue=None,
                    resume_queue=None, limiter=None):
    """Returns (mounted_app, inner_lifespan). mounted_app is wrapped with the
    bearer auth middleware; inner_lifespan must be entered by the parent app so
    the MCP session manager starts. drive_queue (optional) receives round_ids
    to drive after successful submissions. resume_queue (optional) receives
    (matter_id, "decide") after a successful decide_item — 裁定 1
    （2026-09-17）：拍板入队与网页同一完结链路。limiter (optional) enables
    PRD 9.1 rate limiting."""
    mcp = FastMCP("mcp-decision-hub")
    register_tools(mcp, session_factory, settings, drive_queue,
                   resume_queue=resume_queue, limiter=limiter)
    inner = mcp.http_app(path="/")
    inner_lifespan = getattr(inner, "lifespan", None) or inner.router.lifespan_context
    return BearerAuthMiddleware(inner, session_factory, settings, limiter), inner_lifespan

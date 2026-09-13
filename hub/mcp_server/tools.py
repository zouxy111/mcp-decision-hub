"""FastMCP tool shells: extract identity, call methods, translate errors.

Tools are added by tasks 19-21; this task only provides the shared plumbing.
Submit-specific rate limit (PRD 9.1) is enforced here to avoid ASGI
body-buffering complexity — same error code, different transport layer.
"""

import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request

from hub.api.errors import ApiError, error_payload
from hub.config import Settings
from hub.domain.rate_limit import RateLimiter, rate_limit_key_submit


def _current_user_id() -> int:
    request = get_http_request()
    return request.state.user_id


def _current_token_id() -> str:
    request = get_http_request()
    return request.state.token_id


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


def _rate_limit_error(retry_after: int) -> ToolError:
    payload = {"error_code": "RATE_LIMITED",
               "message": "submit_output 请求过于频繁，请稍后重试",
               "details": {"retry_after": retry_after}}
    raise ToolError(json.dumps(payload, ensure_ascii=False))


def register_tools(mcp: FastMCP, session_factory, settings: Settings,
                   drive_queue=None, limiter: RateLimiter | None = None) -> None:
    from hub.api import pipeline
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

    @mcp.tool
    def submit_output(
        task_id: str,
        answers: list[dict],
        human_approved: bool,
        approved_at: str,
        content_digest: str,
        idempotency_key: str,
        notes: str | None = None,
    ) -> dict:
        """Submit human-approved output for a task (PRD 9.2/9.3/9.4)."""
        # Submit-specific rate limit (PRD 9.1): 10/min per token
        if limiter is not None:
            sub_key = rate_limit_key_submit(_current_token_id())
            ok, retry = limiter.allow(
                sub_key, limit=settings.rate_limit_submit_per_minute)
            if not ok:
                _rate_limit_error(retry)
        payload = {
            "task_id": task_id,
            "answers": answers,
            "notes": notes,
            "human_approved": human_approved,
            "approved_at": approved_at,
            "content_digest": content_digest,
            "idempotency_key": idempotency_key,
        }
        result = _call(
            session_factory, methods.mcp_submit_output,
            settings=settings, user_id=_current_user_id(), payload=payload,
        )
        round_id = _call(session_factory, pipeline.maybe_drive_round,
                         task_id=task_id)
        if round_id is not None and drive_queue is not None:
            drive_queue.put_nowait(round_id)
        return result

    @mcp.tool
    def get_matter_status(matter_id: str,
                          rounds_before: int | None = None) -> dict:
        """Matter progress for initiator or participant (PRD 3.0)."""
        return _call(
            session_factory, methods.mcp_get_matter_status,
            settings=settings, user_id=_current_user_id(), matter_id=matter_id,
            rounds_before=rounds_before,
        )

    # ---------------- r5Am9i · 立场层工具（语义明确的 4 个） ----------------

    @mcp.tool
    def declare_item(
        title: str,
        question: str,
        participant_ids: list[int],
        background: str = "",
        irreversible: bool = False,
        options: list[str] | None = None,
        overall_deadline: str | None = None,
    ) -> dict:
        """Declare a new item (MCP 不可用时的 REST 对应：POST /items 语义)。
        Caller becomes the initiator; 2-5 participants required (FR-05)."""
        return _call(
            session_factory, methods.mcp_declare_item,
            settings=settings, user_id=_current_user_id(),
            payload={
                "title": title, "question": question, "background": background,
                "participant_ids": participant_ids,
                "irreversible": irreversible, "options": options,
                "overall_deadline": overall_deadline,
            },
        )

    @mcp.tool
    def submit_stance(
        matter_id: str,
        round_number: int,
        stance: str,
        confidence: float,
        position_summary: str,
        rationale_summary: str,
        content_hash: str,
        non_negotiables: list[str] | None = None,
        conditions: list[str] | None = None,
        open_questions: list[str] | None = None,
        depends_on: list[str] | None = None,
        questions_for: list[dict] | None = None,
        disagreement_kind: str | None = None,
        supersedes: str | None = None,
        acting_as: str = "human",
        authority: str | None = None,
        ttl_seconds: int | None = None,
        urgency: str = "normal",
        visibility: str = "participants",
    ) -> dict:
        """Submit this user's stance for a round (enums validated, 422 on
        illegal values; illegal values never reach the database)."""
        payload = {
            "round_number": round_number, "stance": stance,
            "confidence": confidence,
            "position_summary": position_summary,
            "rationale_summary": rationale_summary,
            "non_negotiables": non_negotiables or [],
            "conditions": conditions or [],
            "open_questions": open_questions or [],
            "depends_on": depends_on or [],
            "questions_for": questions_for or [],
            "disagreement_kind": disagreement_kind,
            "supersedes": supersedes, "acting_as": acting_as,
            "authority": authority, "ttl_seconds": ttl_seconds,
            "urgency": urgency, "visibility": visibility,
            "content_hash": content_hash,
        }
        return _call(
            session_factory, methods.mcp_submit_stance,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, payload=payload,
        )

    @mcp.tool
    def read_stance(matter_id: str, target_user_id: int) -> dict:
        """Read a participant's latest stance for the item (404 semantics
        identical to the JSON API; non-members get 404)."""
        return _call(
            session_factory, methods.mcp_read_stance,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, target_user_id=target_user_id,
        )

    @mcp.tool
    def get_summary(matter_id: str) -> dict:
        """Latest ok round summary for the item (five content fields only,
        no identity)."""
        return _call(
            session_factory, methods.mcp_get_summary,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
        )

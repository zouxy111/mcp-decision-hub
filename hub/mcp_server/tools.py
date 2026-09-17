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
                   drive_queue=None, resume_queue=None,
                   limiter: RateLimiter | None = None) -> None:
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

    @mcp.tool
    def decide_item(
        matter_id: str,
        decision: str,
        expected_version: int,
        final_text: str | None = None,
        rationale: str | None = None,
    ) -> dict:
        """Decide the item's resolution draft (initiator only).

        Allowed decisions: approved / modified / rejected. Irreversible items
        are refused — the initiator must decide those on the web page in
        person. REST 对应：POST /api/items/{id}/decide。"""
        result = _call(
            session_factory, methods.mcp_decide_item,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
            payload={
                "decision": decision,
                "expected_version": expected_version,
                "final_text": final_text,
                "rationale": rationale,
            },
        )
        # 裁定 1（2026-09-17）：拍板成功（_call 已 commit）后入
        # resume_queue，与网页路由同一完结链路——此前 MCP 通道只写库
        # 不入队，事项滞留 awaiting_decision（柠檬果 2026-09-17 报的 P0）。
        if resume_queue is not None:
            resume_queue.put_nowait((matter_id, "decide"))
        return result

    @mcp.tool
    def get_digest(matter_id: str) -> dict:
        """One-page status of the item: latest ok summary + item status +
        convergence. No per-person stances, no identity.
        REST 对应：GET /api/items/{id}/digest。

        注：`methods.mcp_get_digest` 自 2026-09-14（提交 `873ff8d`）就存在，
        但直到现在才注册成 MCP 工具——此前只有 HTTP 出口。"""
        return _call(
            session_factory, methods.mcp_get_digest,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
        )

    @mcp.tool
    def list_items(participant: str | None = None,
                   state: str | None = None) -> dict:
        """List the items you can see, newest first.

        participant="me" narrows to items you are a participant of;
        state="open" excludes terminal items. Visibility is derived from your
        own identity only — you cannot ask for someone else's list.
        REST 对应：GET /api/items?participant=me&state=open。"""
        return _call(
            session_factory, methods.mcp_list_items,
            settings=settings, user_id=_current_user_id(),
            participant=participant, state=state,
        )

    @mcp.tool
    def ask_participant(
        matter_id: str,
        target_user_id: int,
        question: str,
        round_number: int | None = None,
    ) -> dict:
        """Ask one participant of the item a targeted follow-up question.

        No quota and no rate limit — by owner ruling (2026-09-14) this channel
        is intentionally uncapped. round_number defaults to the item's current
        round. The question is delivered to that participant's own task
        context (`get_task.directed_questions`), to be answered when they next
        submit a stance. Repeatedly sending the same question is idempotent.
        REST 对应：POST /api/items/{id}/ask。"""
        return _call(
            session_factory, methods.mcp_ask_participant,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
            payload={
                "target_user_id": target_user_id,
                "question": question,
                "round_number": round_number,
            },
        )

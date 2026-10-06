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
                   limiter: RateLimiter | None = None,
                   board_queue=None) -> None:
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
        """Declare a new item (= a message board). Caller becomes the initiator.

        The board is open immediately: no rounds, no tasks. participant_ids may
        be empty — the usual flow is to create the board, then hand out an
        invite link so people join themselves. Use post_message /
        list_messages to collaborate on it.
        REST 对应：POST /api/items。"""
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

    # ------------------------------------------------------------------
    # 留言板（2026-10-04 形态改造）
    #
    # 协作形态从「一轮一轮派题」改成留言板之后，agent 只需要这两个动作：
    # 读回板上所有发言（互相可见），把自己的判断发上去。没有 task_id、
    # 没有 round_number、没有截止时间 —— 所以这两把工具也不接受这些参数。
    # ------------------------------------------------------------------

    @mcp.tool
    def list_messages(matter_id: str, limit: int | None = None) -> dict:
        """Read the item's message board (oldest first) plus participant cards.

        Each message carries its author's self-reported name and responsibility,
        so you know who is speaking and what they own. Returns 404 when you are
        not a member of the item. REST 对应：GET /api/items/{id}/messages。"""
        return _call(
            session_factory, methods.mcp_list_messages,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, limit=limit,
        )

    @mcp.tool
    def post_message(
        matter_id: str,
        content: str,
        human_approved: bool,
        kind: str = "message",
        acting_as: str = "human",
        attachment_name: str | None = None,
        attachment_md: str | None = None,
        reply_to_message_id: str | None = None,
        ask_user_id: int | None = None,
    ) -> dict:
        """Post a message / question / answer to the item's board.

        **Ask the human FIRST.** ``human_approved`` is required: only pass True
        after you have shown the exact text to your user and they said yes.
        Passing False is rejected (422) — never upload on your own judgement.
        Then ask them the five questions (their call, the evidence, what is
        still unclear, what they own, who to ask) before writing anything.

        Write in plain language, no jargon. Long content: put it in
        ``attachment_md`` with ``attachment_name`` ending in .md.

        kind="question" needs ask_user_id (the participant you ask).
        Answering someone: pass reply_to_message_id=<their question's id>.
        kind="decision" only for the item's initiator.
        REST 对应：POST /api/items/{id}/messages。"""
        result = _call(
            session_factory, methods.mcp_post_message,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
            payload={"content": content, "kind": kind, "acting_as": acting_as,
                     "human_approved": human_approved,
                     "attachment_name": attachment_name,
                     "attachment_md": attachment_md,
                     "reply_to_message_id": reply_to_message_id,
                     "ask_user_id": ask_user_id},
        )
        # 有新留言 → 让后台把这块板的滚动总结增量更新一次
        if board_queue is not None:
            board_queue.put_nowait(matter_id)
        return result

    @mcp.tool
    def get_board_summary(matter_id: str) -> dict:
        """Read the board's rolling summary — do this BEFORE list_messages.

        The cloud keeps a live summary of every board (updated after each new
        message). Read it first: it is small, so it saves context and is fast.
        It gives you: summary (what has been said so far), judgement (a rough
        read on the current task), key_points, open_questions, plus
        message_count / covered_messages / status.

        status="stale" means new messages exist that are not summarised yet —
        use this version anyway, or list_messages for the tail.
        Only pull list_messages when you need the exact wording of something.
        document_version / document_count tell you how many summary documents
        exist; read one in full with get_summary_document.
        REST 对应：GET /api/items/{id}/board_summary。"""
        return _call(
            session_factory, methods.mcp_get_board_summary,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
        )

    @mcp.tool
    def list_summary_documents(matter_id: str, limit: int | None = None) -> dict:
        """List the summary documents the cloud has written for this board.

        Every time the cloud finishes a summary round it files one markdown
        document (newest first here, without the body — small on purpose).
        Use this to see how the picture changed over time; read one in full
        with get_summary_document.
        REST 对应：GET /api/items/{id}/board_summary/documents。"""
        return _call(
            session_factory, methods.mcp_list_summary_documents,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, limit=limit,
        )

    @mcp.tool
    def get_summary_document(matter_id: str, version: int | None = None) -> dict:
        """Read one summary document in full (markdown), newest by default.

        The document is self-contained: what has been said, the cloud's rough
        judgement, what is settled, what is still open, and the messages added
        in that round. Good for showing your human "where this stands" without
        pulling the whole board. Pass version=N for an older one.
        REST 对应：GET /api/items/{id}/board_summary/documents[/{version}]。"""
        return _call(
            session_factory, methods.mcp_get_summary_document,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, version=version,
        )

    @mcp.tool
    def request_board_reread(matter_id: str, reason: str | None = None) -> dict:
        """Ask the cloud to RE-READ THE WHOLE BOARD next round (not the delta).

        Normally the cloud only reads the messages added since the last
        summary — cheap and fast. Use this only when someone says the summary
        is wrong or the situation changed: the next round rebuilds it from the
        first message and files a new document. Pass reason= why.
        REST 对应：POST /api/items/{id}/board_summary/reread。"""
        result = _call(
            session_factory, methods.mcp_request_board_reread,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, reason=reason,
        )
        if board_queue is not None:
            board_queue.put_nowait(matter_id)
        return result

    @mcp.tool
    def list_pending_questions(matter_id: str | None = None) -> dict:
        """Pull the questions the cloud has asked YOU (still unanswered).

        This is the pull end of "cloud asks → you handle it locally → upload
        the answer": fetch these, ask your human the five questions, get their
        approval, then upload the reply with
        post_message(reply_to_message_id=<question id>).
        REST 对应：GET /api/questions。"""
        return _call(
            session_factory, methods.mcp_list_pending_questions,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
        )

    # ---------------- 待办事项（v21，2026-10-07） ----------------
    #
    # 这三个工具是为「随时问 agent 项目到哪了」而立的。返回值都做成结论形态
    # （完成率、逾期数、天数都算好），agent 拿到直接转述即可。

    @mcp.tool
    def list_todos(
        assignee: str = "me",
        status: str = "open",
        matter_id: str | None = None,
        include_unassigned: bool = False,
        limit: int = 20,
    ) -> dict:
        """列出待办事项。默认「我该做什么」：assignee=me + 未完成的。

        「我该做什么」是最常见的问题，所以默认值就按这个来：
        - assignee: me（我）| none（还没人认领）| all（全部）
        - status: open（默认）| doing | done | dropped | all
        - matter_id: 只看某个事项，可省
        - include_unassigned: 默认不带出无主待办，避免汇报失焦

        每条带 matter_title、days_left、overdue —— 别自己算，取整和时区
        容易错。REST 对应：GET /api/todos。"""
        return _call(
            session_factory, methods.mcp_list_todos,
            settings=settings, user_id=_current_user_id(),
            assignee=assignee, status=status, matter_id=matter_id,
            include_unassigned=include_unassigned, limit=limit,
        )

    @mcp.tool
    def get_project_status(matter_id: str) -> dict:
        """项目全貌：这个事项现在到哪了。

        一次返回进度（完成率已算好）、按人分组（谁手上多、谁快逾期）、
        近期变动、以及风险（取自留言总结里「还没解决的问题」）。
        还有一句headline 结论，可以直接转述给用户。

        想知道「我该做什么」用 list_todos；想知道「整体什么情况」用这个。
        REST 对应：GET /api/matters/{id}/project_status。"""
        return _call(
            session_factory, methods.mcp_get_project_status,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id,
        )

    @mcp.tool
    def create_todo(
        matter_id: str,
        title: str,
        detail: str | None = None,
        assignee_id: int | None = None,
        due_at: str | None = None,
    ) -> dict:
        """新建一条待办。

        人在网页上建和你调这个建是同一条路径。你是在人明确指示下写的，
        所以建出来直接是正式待办，不需要再让人确认（只有 AI 自己抽的才要）。

        assignee_id 必须是该事项的参与人；不确定就别指派，留给后人认领。
        due_at 用 ISO 格式（2026-10-20T18:00:00Z）。
        REST 对应：POST /api/matters/{id}/todos。"""
        return _call(
            session_factory, methods.mcp_create_todo,
            settings=settings, user_id=_current_user_id(),
            matter_id=matter_id, title=title, detail=detail,
            assignee_id=assignee_id, due_at=due_at,
        )

    @mcp.tool
    def update_todo(
        todo_id: str,
        status: str | None = None,
        assignee_id: int | None = None,
        due_at: str | None = None,
        title: str | None = None,
    ) -> dict:
        """改一条待办（状态 / 指派人 / 截止日 / 标题）。不传的字段不动。

        典型用法是标完成：update_todo(todo_id=..., status="done")。
        非法状态转移会报错（如 dropped → done），不会静默改坏。
        REST 对应：PATCH /api/todos/{id}。"""
        return _call(
            session_factory, methods.mcp_update_todo,
            settings=settings, user_id=_current_user_id(),
            todo_id=todo_id, status=status, assignee_id=assignee_id,
            due_at=due_at, title=title,
        )

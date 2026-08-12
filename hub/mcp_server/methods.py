"""Business implementation of the 4 MCP methods. Filled in by tasks 19-21."""

import base64
import json
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.api.matters import is_participant
from hub.api.passwords import sha256_hex
from hub.config import Settings
from hub.db.models import (
    IdempotencyRecord,
    Matter,
    Output,
    Resolution,
    Round,
    RoundSummary,
    Task,
    User,
)
from hub.domain.approval import ApprovalWindowError, validate_approved_at
from hub.domain.digest import compute_content_digest
from hub.domain.idempotency import IdempotencyDecision, decide_idempotency
from hub.domain.limits import (
    ContentLimitError,
    validate_content_limits,
    validate_request_body_size,
)
from hub.domain.timeutil import iso_z, parse_iso_z, utcnow

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
POLL_SECONDS_IDLE = 300
POLL_SECONDS_ACTIVE = 30


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as e:
        raise ApiError(422, "CURSOR_INVALID", "cursor 无法解析") from e


def mcp_list_pending_tasks(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    effective_limit = DEFAULT_LIMIT if limit is None else min(max(1, limit), MAX_LIMIT)
    offset = _decode_cursor(cursor) if cursor else 0
    rows = session.execute(
        select(Task, Matter.title, Round.round_number)
        .join(Matter, Task.matter_id == Matter.id)
        .join(Round, Task.round_id == Round.id)
        .where(Task.assignee_id == user_id, Task.status == "pending")
        .order_by(Task.created_at, Task.id)
        .offset(offset)
        .limit(effective_limit + 1)
    ).all()
    has_more = len(rows) > effective_limit
    rows = rows[:effective_limit]
    poll_seconds = POLL_SECONDS_ACTIVE if rows else POLL_SECONDS_IDLE
    return {
        "tasks": [
            {
                "task_id": task.id,
                "matter_id": task.matter_id,
                "matter_title": title,
                "round_number": round_number,
                "deadline_at": iso_z(task.deadline_at) if task.deadline_at else None,
                "created_at": iso_z(task.created_at),
            }
            for task, title, round_number in rows
        ],
        "next_cursor": _encode_cursor(offset + effective_limit) if has_more else None,
        "next_poll_after": iso_z(utcnow() + timedelta(seconds=poll_seconds)),
    }


def mcp_get_task(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    task_id: str,
) -> dict:
    task = session.get(Task, task_id)
    if task is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    if task.assignee_id != user_id:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "get_task", "task_id": task_id},
        )
        raise ApiError(403, "FORBIDDEN_SCOPE", "无权访问该任务")
    matter = session.get(Matter, task.matter_id)
    rnd = session.get(Round, task.round_id)
    return {
        "task_id": task.id,
        "status": task.status,
        "matter": {
            "matter_id": matter.id,
            "title": matter.title,
            "background": matter.background,
            "goal": matter.goal,
        },
        "round": {
            "round_id": rnd.id,
            "round_number": rnd.round_number,
            "questions": rnd.questions,
        },
        "previous_summary": _previous_summary_view(session, rnd),
        "deadline_at": iso_z(task.deadline_at) if task.deadline_at else None,
        "llm_provider": settings.llm_provider_name,
    }


def _limit_api_error(e: ContentLimitError) -> ApiError:
    return ApiError(
        422, "CONTENT_LIMIT_EXCEEDED", str(e),
        details={"limit": e.limit_name, "actual": e.actual, "max": e.limit},
    )


def _canonical_answers(answers: list[dict]) -> str:
    return json.dumps(
        sorted([[a["question_id"], a["content"]] for a in answers]),
        ensure_ascii=False,
    )


def _validate_answers_shape(answers, valid_question_ids: set[str]) -> None:
    if not isinstance(answers, list) or len(answers) == 0:
        raise ApiError(422, "QUESTION_INVALID", "answers 为空或缺失")
    seen: set[str] = set()
    for item in answers:
        if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
            raise ApiError(422, "QUESTION_INVALID", "answers 项缺少 question_id")
        if not isinstance(item.get("content"), str) or not item["content"]:
            raise ApiError(422, "QUESTION_INVALID", "answers 项 content 为空")
        qid = item["question_id"]
        if qid in seen:
            raise ApiError(422, "QUESTION_INVALID", f"重复题号: {qid}")
        if qid not in valid_question_ids:
            raise ApiError(422, "QUESTION_INVALID", f"未知或非本任务题号: {qid}")
        seen.add(qid)


def mcp_submit_output(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    payload: dict,
) -> dict:
    received_at = utcnow()
    try:
        validate_request_body_size(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            body_limit=settings.request_body_limit,
        )
    except ContentLimitError as e:
        raise _limit_api_error(e) from e

    task_id = payload.get("task_id")
    idempotency_key = payload.get("idempotency_key")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError(422, "VALIDATION_FAILED", "task_id 缺失或类型错误")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ApiError(422, "VALIDATION_FAILED", "idempotency_key 为必填")

    task = session.get(Task, task_id)
    if task is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    if task.assignee_id != user_id:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "submit_output", "task_id": task_id},
        )
        raise ApiError(403, "FORBIDDEN_SCOPE", "无权向该任务提交")

    rnd = session.get(Round, task.round_id)
    valid_qids = {q["question_id"] for q in rnd.questions}
    answers = payload.get("answers")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ApiError(422, "VALIDATION_FAILED", "notes 类型错误")
    _validate_answers_shape(answers, valid_qids)
    try:
        validate_content_limits(
            answers, notes,
            item_limit=settings.content_item_limit,
            total_limit=settings.content_total_limit,
            notes_limit=settings.notes_limit,
        )
    except ContentLimitError as e:
        raise _limit_api_error(e) from e

    if payload.get("human_approved") is not True:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "缺少 human_approved: true 人审声明")
    approved_at_raw = payload.get("approved_at")
    if not isinstance(approved_at_raw, str):
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED", "approved_at 缺失或类型错误")
    try:
        approved_at = parse_iso_z(approved_at_raw)
    except ValueError as e:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "approved_at 不是合法 ISO 8601 时间") from e
    try:
        validate_approved_at(approved_at, received_at)
    except ApprovalWindowError as e:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED", str(e)) from e
    digest = payload.get("content_digest")
    if not isinstance(digest, str) or digest != compute_content_digest(answers, notes):
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "content_digest 与提交正文不匹配（摘要不匹配）")

    fingerprint = sha256_hex(
        json.dumps(payload, sort_keys=True, ensure_ascii=False)
    )
    record = session.get(IdempotencyRecord, (task_id, idempotency_key))
    existing_output = session.scalar(
        select(Output).where(Output.task_id == task_id)
    )
    decision = decide_idempotency(
        task_status=task.status,
        key_seen=record is not None,
        key_body_matches=(
            record.request_fingerprint == fingerprint if record else None
        ),
        content_matches=(
            (
                _canonical_answers(existing_output.answers)
                == _canonical_answers(answers)
                and (existing_output.notes or "") == (notes or "")
            )
            if existing_output
            else None
        ),
    )

    if decision is IdempotencyDecision.REPLAY_SAME_KEY:
        return json.loads(record.response_json)
    if decision is IdempotencyDecision.CONFLICT_SAME_KEY:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等键对应不同请求体")
    if decision is IdempotencyDecision.INVALID_STATE:
        audit.record_audit(
            session, audit.INVALID_STATE_TRANSITION, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "submit_output", "task_id": task_id,
                    "task_status": task.status},
        )
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"任务状态 {task.status} 不允许提交")
    if decision is IdempotencyDecision.REPLAY_EQUIVALENT:
        audit.record_audit(
            session, audit.OUTPUT_REPLAYED, actor_user_id=user_id,
            matter_id=task.matter_id, detail={"task_id": task_id},
        )
        return {
            "task_id": task_id,
            "status": "submitted",
            "submitted_at": iso_z(task.submitted_at),
            "output_id": existing_output.id,
        }
    if decision is IdempotencyDecision.ALREADY_SUBMITTED:
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在不同内容的提交")

    # CREATE_NEW: conditional UPDATE first so a lost race never overwrites.
    result = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "pending")
        .values(status="submitted", submitted_at=received_at)
    )
    if result.rowcount != 1:
        session.rollback()
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在其他提交（并发竞争）")
    output = Output(
        task_id=task_id, answers=answers, notes=notes,
        approved_at=approved_at, content_digest=digest,
    )
    session.add(output)
    session.flush()
    response = {
        "task_id": task_id,
        "status": "submitted",
        "submitted_at": iso_z(received_at),
        "output_id": output.id,
    }
    session.add(
        IdempotencyRecord(
            task_id=task_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response_json=json.dumps(response, ensure_ascii=False),
        )
    )
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在其他提交（唯一约束）") from e
    audit.record_audit(
        session, audit.TASK_SUBMITTED, actor_user_id=user_id,
        matter_id=task.matter_id, detail={"task_id": task_id},
    )
    return response


def mcp_get_matter_status(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    rounds_before: int | None = None,
) -> dict:
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    involved = matter.initiator_id == user_id or is_participant(
        session, matter_id=matter_id, user_id=user_id
    )
    if not involved:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=matter_id, detail={"action": "get_matter_status"},
        )
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在或不可见")
    all_rounds = session.scalars(
        select(Round)
        .where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc())
    ).all()
    visible = all_rounds
    if rounds_before is not None:
        visible = [r for r in visible if r.round_number < rounds_before]
    recent = visible[:3]
    round_views = []
    for rnd in recent:
        tasks = session.scalars(
            select(Task).where(Task.round_id == rnd.id)
        ).all()
        round_views.append({
            "round_id": rnd.id,
            "round_number": rnd.round_number,
            "status": rnd.status,
            "tasks_total": len(tasks),
            "tasks_submitted": sum(1 for t in tasks if t.status == "submitted"),
            "summaries": _round_summaries_view(session, rnd.id),
        })
    result = {
        "matter_id": matter.id,
        "title": matter.title,
        "status": matter.status,
        "rounds_total": len(all_rounds),
        "recent_rounds": round_views,
        "resolution": _resolution_view(session, matter.id),
    }
    if matter.initiator_id == user_id and round_views:
        latest = round_views[0]
        tasks = session.scalars(
            select(Task).where(Task.round_id == latest["round_id"])
        ).all()
        result["participant_progress"] = [
            {
                "user_id": t.assignee_id,
                "username": session.get(User, t.assignee_id).username,
                "task_id": t.id,
                "task_status": t.status,
            }
            for t in tasks
        ]
    return result


def _summary_payload(summary: RoundSummary) -> dict:
    return {
        "consensus_points": summary.consensus_points,
        "divergences": summary.divergences,
        "blind_spots": summary.blind_spots,
        "open_questions": summary.open_questions,
        "convergence": summary.convergence,
    }


def _previous_summary_view(session: Session, rnd: Round) -> dict | None:
    """Previous round's ok summary for get_task (PRD 9.2). None for round 1
    or when the previous round has no ok summary; never fabricated."""
    if rnd.round_number <= 1:
        return None
    prev_round = session.scalar(
        select(Round).where(Round.matter_id == rnd.matter_id,
                            Round.round_number == rnd.round_number - 1)
    )
    if prev_round is None:
        return None
    prev = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == prev_round.id,
                                   RoundSummary.generation_status == "ok")
    )
    if prev is None:
        return None
    return {"round_number": prev_round.round_number, **_summary_payload(prev)}


def _resolution_view(session: Session, matter_id: str) -> dict | None:
    """Resolution stage and version for get_matter_status (PRD 9.2). Same
    view for initiator and participants; draft/final bodies stay on the web
    decision page. None when no resolution exists."""
    res = session.scalar(
        select(Resolution)
        .where(Resolution.matter_id == matter_id)
        .order_by(Resolution.version.desc())
        .limit(1)
    )
    if res is None:
        return None
    return {
        "resolution_id": res.id,
        "status": res.status,
        "version": res.version,
        "cited_rounds": res.cited_rounds,
        "created_at": iso_z(res.created_at),
        "decided_at": iso_z(res.decided_at) if res.decided_at else None,
    }


def _round_summaries_view(session: Session, round_id: str) -> list[dict]:
    """Real ok summaries for a round (at most one row by unique constraint);
    visible to initiator AND participants (FR-07). Empty list when absent."""
    ok = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == round_id,
                                   RoundSummary.generation_status == "ok")
    )
    if ok is None:
        return []
    return [{
        "summary_id": ok.id,
        **_summary_payload(ok),
        "created_at": iso_z(ok.created_at),
    }]

"""Business implementation of the 4 MCP methods. Filled in by tasks 19-21."""

import base64
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.config import Settings
from hub.db.models import Matter, Round, Task
from hub.domain.timeutil import iso_z, utcnow

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
        "previous_summary": None,  # M1: summaries land in M2; never fabricate
        "deadline_at": iso_z(task.deadline_at) if task.deadline_at else None,
        "llm_provider": settings.llm_provider_name,
    }

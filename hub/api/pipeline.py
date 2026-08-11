"""Collection-driven round pipeline (PRD 6.3.1 / FR-14~FR-18 / 7.5 / 7.6).

Sync SQLAlchemy throughout. LLM calls happen only inside run_round_pipeline,
which is executed by the background worker — never in request paths
(constraint 10, PRD 11.2). All state transitions use conditional UPDATEs and
all LLM artifacts are idempotent (constraint 11).
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.db.models import Matter, Round, Task
from hub.domain.collection import is_round_collected
from hub.domain.timeutil import utcnow

BLOCKED_REASON_NO_OUTPUT = "本轮无有效输出"
BLOCKED_REASON_ROUND_LIMIT = "达到轮次上限"
BLOCKED_REASON_SUMMARY_FAILED = "摘要生成失败（LLM 重试耗尽）"
BLOCKED_REASON_FOLLOWUP_FAILED = "定向追问出题失败（LLM 重试耗尽）"
BLOCKED_REASON_LLM_BLOCKED = "收敛判定为 blocked"


def maybe_drive_round(session: Session, *, task_id: str) -> str | None:
    """Entry point called after a successful submit_output. If the round is
    now collected, flip round open→awaiting_summary and matter
    collecting→in_progress with conditional UPDATEs and return the round_id
    to drive in the background; otherwise return None."""
    task = session.get(Task, task_id)
    if task is None:
        return None
    rnd = session.get(Round, task.round_id)
    if rnd is None or rnd.status != "open":
        return None
    statuses = list(
        session.scalars(select(Task.status).where(Task.round_id == rnd.id)).all()
    )
    if not is_round_collected(statuses):
        return None
    result = session.execute(
        update(Round)
        .where(Round.id == rnd.id, Round.status == "open")
        .values(status="awaiting_summary")
    )
    if result.rowcount != 1:
        return None  # another driver won the race
    session.execute(
        update(Matter)
        .where(Matter.id == rnd.matter_id, Matter.status == "collecting")
        .values(status="in_progress", updated_at=utcnow())
    )
    return rnd.id

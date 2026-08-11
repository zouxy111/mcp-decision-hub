"""Collection-driven round pipeline (PRD 6.3.1 / FR-14~FR-18 / 7.5 / 7.6).

Sync SQLAlchemy throughout. LLM calls happen only inside run_round_pipeline,
which is executed by the background worker — never in request paths
(constraint 10, PRD 11.2). All state transitions use conditional UPDATEs and
all LLM artifacts are idempotent (constraint 11).
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.config import Settings
from hub.db.models import Matter, Output, Round, RoundSummary, Task
from hub.domain.collection import count_submitted, is_round_collected
from hub.domain.timeutil import utcnow
from hub.llm.client import LLMError
from hub.llm.prompts import build_round_summary_prompt

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


def run_round_pipeline(
    session_factory, settings: Settings, *, round_id: str, llm
) -> None:
    """Background driver for one round. Two phases with separate commits so a
    crash between them stays recoverable (reconciler, task 11). Idempotent:
    safe to call repeatedly for the same round."""
    with session_factory() as session:
        rnd = session.get(Round, round_id)
        if rnd is not None and rnd.status == "awaiting_summary":
            _summarize_phase(session, rnd, llm)
        session.commit()
    with session_factory() as session:
        rnd = session.get(Round, round_id)
        if rnd is not None:
            _branch_phase(session, rnd, llm)
        session.commit()


def _block_matter(session: Session, matter: Matter, reason: str) -> None:
    session.execute(
        update(Matter)
        .where(Matter.id == matter.id, Matter.status == "in_progress")
        .values(status="blocked", blocked_reason=reason, updated_at=utcnow())
    )
    audit.record_audit(session, audit.MATTER_BLOCKED, matter_id=matter.id,
                       detail={"reason": reason})


def _submissions_for_llm(session: Session, tasks: list[Task]) -> list[dict]:
    """PRD 4.3 whitelist only: answers and notes. No usernames or ids."""
    submissions = []
    for task in tasks:
        if task.status != "submitted":
            continue
        out = session.scalar(select(Output).where(Output.task_id == task.id))
        if out is None:
            continue
        submissions.append({"answers": out.answers, "notes": out.notes})
    return submissions


def _previous_summary_dict(session: Session, rnd: Round) -> dict | None:
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
    return _summary_to_dict(prev)


def _summary_to_dict(summary: RoundSummary) -> dict:
    return {
        "consensus_points": summary.consensus_points,
        "divergences": summary.divergences,
        "blind_spots": summary.blind_spots,
        "open_questions": summary.open_questions,
        "convergence": summary.convergence,
    }


def _summarize_phase(session: Session, rnd: Round, llm) -> None:
    matter = session.get(Matter, rnd.matter_id)
    existing = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == rnd.id)
    )
    if existing is not None:
        return  # ok → already generated (idempotent); failed → terminal
    tasks = list(session.scalars(select(Task).where(Task.round_id == rnd.id)).all())
    if count_submitted([t.status for t in tasks]) == 0:
        # PRD 6.3.1: no effective output — never call LLM with empty input
        session.execute(
            update(Round)
            .where(Round.id == rnd.id, Round.status == "awaiting_summary")
            .values(status="closed", closed_at=utcnow())
        )
        _block_matter(session, matter, BLOCKED_REASON_NO_OUTPUT)
        return
    system_prompt, user_prompt = build_round_summary_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
        questions=rnd.questions,
        submissions=_submissions_for_llm(session, tasks),
        previous_summary=_previous_summary_dict(session, rnd),
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name="round_summary")
    except LLMError as e:
        session.add(
            RoundSummary(
                round_id=rnd.id, matter_id=matter.id,
                consensus_points=[], divergences=[], blind_spots=[],
                open_questions=[], convergence=None,
                generation_status="failed",
                error_code=e.error_code, retry_count=e.retry_count,
            )
        )
        session.execute(
            update(Round)
            .where(Round.id == rnd.id, Round.status == "awaiting_summary")
            .values(status="failed")
        )
        _block_matter(session, matter, BLOCKED_REASON_SUMMARY_FAILED)
        audit.record_audit(session, audit.LLM_FAILED, matter_id=matter.id,
                           detail={"stage": "round_summary", "round_id": rnd.id,
                                   "error_code": e.error_code,
                                   "retry_count": e.retry_count})
        return
    session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=data["consensus_points"],
            divergences=data["divergences"],
            blind_spots=data["blind_spots"],
            open_questions=data["open_questions"],
            convergence=data["convergence"],
            generation_status="ok",
        )
    )
    session.execute(
        update(Round)
        .where(Round.id == rnd.id, Round.status == "awaiting_summary")
        .values(status="closed", closed_at=utcnow())
    )
    audit.record_audit(session, audit.ROUND_SUMMARIZED, matter_id=matter.id,
                       detail={"round_id": rnd.id,
                               "round_number": rnd.round_number})
    audit.record_audit(session, audit.CONVERGENCE_DECIDED, matter_id=matter.id,
                       detail={"round_id": rnd.id,
                               "convergence": data["convergence"]})


def _branch_phase(session: Session, rnd: Round, llm) -> None:
    """Task 10 implements the convergence branch; placeholder keeps the
    two-phase structure callable now."""
    return None

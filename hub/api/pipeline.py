"""Collection-driven round pipeline (PRD 6.3.1 / FR-14~FR-18 / 7.5 / 7.6).

Sync SQLAlchemy throughout. LLM calls happen only inside run_round_pipeline,
which is executed by the background worker — never in request paths
(constraint 10, PRD 11.2). All state transitions use conditional UPDATEs and
all LLM artifacts are idempotent (constraint 11).
"""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.config import Settings
from hub.db.models import (
    Matter,
    MatterParticipant,
    Output,
    Round,
    RoundSummary,
    Task,
)
from hub.domain.collection import count_submitted, is_round_collected
from hub.domain.convergence import (
    CONVERGENCE_BLOCKED,
    CONVERGENCE_CONTINUE,
    CONVERGENCE_CONVERGED,
    CONVERGENCE_PROVISIONALLY_READY,
)
from hub.domain.credits import can_auto_advance
from hub.domain.timeutil import utcnow
from hub.llm.client import MAX_RETRIES, LLMError
from hub.llm.prompts import (
    build_followup_questions_prompt,
    build_generate_questions_prompt,
    build_round_summary_prompt,
)

BLOCKED_REASON_NO_OUTPUT = "本轮无有效输出"
BLOCKED_REASON_ROUND_LIMIT = "达到轮次上限"
BLOCKED_REASON_SUMMARY_FAILED = "摘要生成失败（LLM 重试耗尽）"
BLOCKED_REASON_FOLLOWUP_FAILED = "定向追问出题失败（LLM 重试耗尽）"
BLOCKED_REASON_LLM_BLOCKED = "收敛判定为 blocked"
BLOCKED_REASON_FIRST_ROUND_FAILED = "首轮出题失败（LLM 重试耗尽）"


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
        if rnd is not None and rnd.status == "generating" and not rnd.questions:
            _generate_first_round_phase(session, rnd, llm)
        elif rnd is not None and rnd.status == "awaiting_summary":
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
    """Convergence branch (PRD 7.5/7.6). Idempotent: only ever branches from
    the matter's latest round, only while the matter is in_progress, and
    never creates round_number+1 twice."""
    matter = session.get(Matter, rnd.matter_id)
    if matter.status != "in_progress":
        return
    if rnd.status != "closed":
        return
    summary = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == rnd.id,
                                   RoundSummary.generation_status == "ok")
    )
    if summary is None:
        return
    latest = session.scalar(
        select(Round).where(Round.matter_id == matter.id)
        .order_by(Round.round_number.desc()).limit(1)
    )
    if latest is None or latest.id != rnd.id:
        return
    convergence = summary.convergence
    if convergence == CONVERGENCE_BLOCKED:
        _block_matter(session, matter, BLOCKED_REASON_LLM_BLOCKED)
        return
    if convergence in (CONVERGENCE_PROVISIONALLY_READY, CONVERGENCE_CONVERGED):
        # M2: both go to awaiting_decision; decision drafts land in M3.
        session.execute(
            update(Matter)
            .where(Matter.id == matter.id, Matter.status == "in_progress")
            .values(status="awaiting_decision", blocked_reason=None,
                    updated_at=utcnow())
        )
        return
    if convergence != CONVERGENCE_CONTINUE:
        return  # defensive: client schema validation guarantees one of four
    if not can_auto_advance(
        current_round_number=rnd.round_number,
        max_rounds=matter.max_rounds,
        granted_extra_rounds=matter.granted_extra_rounds,
    ):
        _block_matter(session, matter, BLOCKED_REASON_ROUND_LIMIT)
        return
    exists_next = session.scalar(
        select(Round.id).where(Round.matter_id == matter.id,
                               Round.round_number == rnd.round_number + 1)
    )
    if exists_next is not None:
        return
    system_prompt, user_prompt = build_followup_questions_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
        summary=_summary_to_dict(summary),
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name="questions")
    except LLMError as e:
        _block_matter(
            session, matter,
            f"{BLOCKED_REASON_FOLLOWUP_FAILED}：{e.error_code}"
            f"（已重试 {e.retry_count} 次）",
        )
        audit.record_audit(session, audit.LLM_FAILED, matter_id=matter.id,
                           detail={"stage": "followup_questions",
                                   "round_id": rnd.id,
                                   "error_code": e.error_code,
                                   "retry_count": e.retry_count})
        return
    # question_id is a server-side identifier; the LLM only provides content
    # (PRD 9.1).
    questions = [
        {"question_id": f"q{i + 1}", "content": content}
        for i, content in enumerate(data["questions"])
    ]
    new_round = Round(
        matter_id=matter.id, round_number=rnd.round_number + 1,
        status="generating", questions=questions,
    )
    session.add(new_round)
    session.flush()
    participant_ids = session.scalars(
        select(MatterParticipant.user_id)
        .where(MatterParticipant.matter_id == matter.id)
    ).all()
    deadline = utcnow() + timedelta(seconds=matter.timeout_seconds)
    for uid in participant_ids:
        session.add(
            Task(round_id=new_round.id, matter_id=matter.id, assignee_id=uid,
                 status="pending", deadline_at=deadline)
        )
    new_round.status = "open"
    session.execute(
        update(Matter)
        .where(Matter.id == matter.id, Matter.status == "in_progress")
        .values(status="collecting", blocked_reason=None, updated_at=utcnow())
    )
    audit.record_audit(session, audit.ROUND_GENERATED, matter_id=matter.id,
                       detail={"round_id": new_round.id,
                               "round_number": new_round.round_number,
                               "task_count": len(participant_ids)})


def find_interrupted_round_ids(session: Session) -> list[str]:
    """Startup recovery scan (FR-24, M2 scope). Returns round_ids that must
    be re-driven. Safe to run repeatedly: re-driving is idempotent."""
    ids: list[str] = []
    # (a0) rounds stuck in 'generating' with empty questions — first-round
    # LLM generation interrupted before or during the LLM call (task 13)
    generating = list(
        session.scalars(select(Round).where(Round.status == "generating")).all()
    )
    for rnd in generating:
        if not rnd.questions:
            ids.append(rnd.id)
    # (a) rounds stuck in awaiting_summary without a usable summary
    awaiting = list(
        session.scalars(select(Round).where(Round.status == "awaiting_summary"))
        .all()
    )
    for rnd in awaiting:
        ok_exists = session.scalar(
            select(RoundSummary.id).where(
                RoundSummary.round_id == rnd.id,
                RoundSummary.generation_status == "ok",
            )
        )
        if ok_exists is not None:
            continue
        failed = session.scalar(
            select(RoundSummary).where(
                RoundSummary.round_id == rnd.id,
                RoundSummary.generation_status == "failed",
            )
        )
        if failed is None or (failed.retry_count or 0) < MAX_RETRIES:
            ids.append(rnd.id)
    # (b) matters in_progress with no active round (branch phase interrupted)
    matters = list(
        session.scalars(select(Matter).where(Matter.status == "in_progress")).all()
    )
    for matter in matters:
        active = session.scalar(
            select(Round.id).where(
                Round.matter_id == matter.id,
                Round.status.in_(("generating", "open", "awaiting_summary")),
            )
        )
        if active is not None:
            continue
        latest = session.scalar(
            select(Round).where(Round.matter_id == matter.id)
            .order_by(Round.round_number.desc()).limit(1)
        )
        if latest is not None:
            ids.append(latest.id)
    return list(dict.fromkeys(ids))


def _generate_first_round_phase(session: Session, rnd: Round, llm) -> None:
    """First-round question generation (FR-05, scenario 12). Only runs for a
    'generating' round with empty questions. The first round never consumes
    round credits: PRD 7.5 limits auto-advance only (see _branch_phase)."""
    matter = session.get(Matter, rnd.matter_id)
    system_prompt, user_prompt = build_generate_questions_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name="questions")
    except LLMError as e:
        session.execute(
            update(Round)
            .where(Round.id == rnd.id, Round.status == "generating")
            .values(status="failed")
        )
        _block_matter(
            session, matter,
            f"{BLOCKED_REASON_FIRST_ROUND_FAILED}：{e.error_code}"
            f"（已重试 {e.retry_count} 次）",
        )
        audit.record_audit(session, audit.LLM_FAILED, matter_id=matter.id,
                           detail={"stage": "first_round_questions",
                                   "round_id": rnd.id,
                                   "error_code": e.error_code,
                                   "retry_count": e.retry_count})
        return
    # question_id 是服务端标识符；LLM 只提供问题文本（PRD 9.1）
    questions = [
        {"question_id": f"q{i + 1}", "content": content}
        for i, content in enumerate(data["questions"])
    ]
    result = session.execute(
        update(Round)
        .where(Round.id == rnd.id, Round.status == "generating")
        .values(questions=questions)
    )
    if result.rowcount != 1:
        return  # 轮次状态并发变化；由 reconciler 重新评估
    participant_ids = session.scalars(
        select(MatterParticipant.user_id)
        .where(MatterParticipant.matter_id == matter.id)
    ).all()
    deadline = utcnow() + timedelta(seconds=matter.timeout_seconds)
    for uid in participant_ids:
        session.add(
            Task(round_id=rnd.id, matter_id=matter.id, assignee_id=uid,
                 status="pending", deadline_at=deadline)
        )
    session.execute(
        update(Round)
        .where(Round.id == rnd.id, Round.status == "generating")
        .values(status="open")
    )
    session.execute(
        update(Matter)
        .where(Matter.id == matter.id, Matter.status == "in_progress")
        .values(status="collecting", updated_at=utcnow())
    )
    audit.record_audit(session, audit.ROUND_GENERATED, matter_id=matter.id,
                       detail={"round_id": rnd.id,
                               "round_number": rnd.round_number,
                               "task_count": len(participant_ids),
                               "mode": "llm_generate"})

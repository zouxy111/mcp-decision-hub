"""Collection-driven round pipeline (PRD 6.3.1 / FR-14~FR-18 / 7.5 / 7.6).

Sync SQLAlchemy throughout. LLM calls happen only inside run_round_pipeline,
which is executed by the background worker — never in request paths
(constraint 10, PRD 11.2). All state transitions use conditional UPDATEs and
all LLM artifacts are idempotent (constraint 11).
"""

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.config import Settings
from hub.db.models import (
    Matter,
    MatterParticipant,
    Output,
    Resolution,
    Round,
    RoundSummary,
    Stance,
    Task,
)
from hub.domain import stall_detect as stall
from hub.domain.collection import count_submitted, is_round_collected
from hub.domain.convergence import (
    CONVERGENCE_BLOCKED,
    CONVERGENCE_CONTINUE,
    CONVERGENCE_CONVERGED,
    CONVERGENCE_PROVISIONALLY_READY,
)
from hub.domain.credits import CREDIT_GRANT_PER_CONTINUE, can_auto_advance
from hub.domain.resolution import (
    RESOLUTION_STATUS_APPROVED,
    RESOLUTION_STATUS_MODIFIED,
    RESOLUTION_TERMINAL_STATUSES,
)
from hub.domain.timeutil import utcnow
from hub.llm.client import MAX_RETRIES, LLMError
from hub.llm.prompts import (
    build_followup_questions_prompt,
    build_generate_questions_prompt,
    build_resolution_draft_prompt,
    build_round_summary_prompt,
)

BLOCKED_REASON_NO_OUTPUT = "本轮无有效输出"
BLOCKED_REASON_ROUND_LIMIT = "达到轮次上限"
BLOCKED_REASON_SUMMARY_FAILED = "摘要生成失败（LLM 重试耗尽）"
BLOCKED_REASON_FOLLOWUP_FAILED = "定向追问出题失败（LLM 重试耗尽）"
BLOCKED_REASON_LLM_BLOCKED = "收敛判定为 blocked"
BLOCKED_REASON_FIRST_ROUND_FAILED = "首轮出题失败（LLM 重试耗尽）"
BLOCKED_REASON_DRAFT_FAILED = "决议草案生成失败（LLM 重试耗尽）"
# rUdiTJ 片 1：不收敛且本轮相对上轮无任何内容差异 → 判僵持、立即拉人，不空转。
BLOCKED_REASON_STALLED = "无新增信息（僵持）"


def resolve_round_matter(session: Session, *, round_id: str) -> str | None:
    """轻量查询：返回 round_id 所属的 matter_id（供 background matter 锁用）。"""
    rnd = session.get(Round, round_id)
    return rnd.matter_id if rnd is not None else None


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
    """唯一后台驱动入口（M2 签名不变）。M3 起内部经 LangGraph tick 驱动：
    round_id 解析出 matter_id 后交给 drive_matter_tick；图的相位节点与 M2
    相位函数一一对应，幂等语义不变。"""
    with session_factory() as session:
        rnd = session.get(Round, round_id)
        matter_id = rnd.matter_id if rnd is not None else None
    if matter_id is None:
        return
    from hub.graph.matter_graph import drive_matter_tick

    drive_matter_tick(session_factory, settings, matter_id=matter_id, llm=llm)


def _block_matter(session: Session, matter: Matter, reason: str, *,
                  extra: dict | None = None) -> None:
    session.execute(
        update(Matter)
        .where(Matter.id == matter.id, Matter.status == "in_progress")
        .values(status="blocked", blocked_reason=reason, updated_at=utcnow())
    )
    audit.record_audit(session, audit.MATTER_BLOCKED, matter_id=matter.id,
                       detail={"reason": reason, **(extra or {})})


def _stances_of_round(session: Session, *, matter_id: str,
                      round_number: int) -> dict:
    """某一轮的立场快照（按 user_id 归集）。轮次不存在/无立场 → 空 dict。"""
    rows = session.scalars(
        select(Stance).where(Stance.matter_id == matter_id,
                             Stance.round_number == round_number)
    ).all()
    return stall.snapshots_from_rows(rows)


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
        # M3: 不在这里翻状态；决议草案与 awaiting_decision 翻转由
        # _draft_resolution_phase 负责（FR-19/7.6）。
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
    # rUdiTJ 片 1（验收切片）：达成「本可以开下一轮」之后、真开之前，先问一句
    # 「本轮相对上轮有没有新增信息」。没有 → 判僵持、立即拉人、**不开下一轮**。
    # 「拉人」不另起一套：置 blocked 后由发起人走既有换人流程
    # （hub/api/reassignment.py，FR-16），与轮次上限那条路径同一出口。
    verdict = stall.detect_stall(
        _stances_of_round(session, matter_id=matter.id,
                          round_number=rnd.round_number - 1),
        _stances_of_round(session, matter_id=matter.id,
                          round_number=rnd.round_number),
    )
    if verdict.stalled:
        _block_matter(session, matter, BLOCKED_REASON_STALLED,
                      extra={"round_number": rnd.round_number,
                             "stall_detail": verdict.reason})
        return
    _open_followup_round(session, matter, rnd, summary, llm)


def _open_followup_round(session: Session, matter: Matter, rnd: Round,
                         summary: RoundSummary, llm) -> bool:
    """Generate targeted follow-up questions and open round_number+1 (FR-17).
    Caller guarantees credit. Idempotent: never creates the next round twice.
    Returns True iff a new round was created. On LLM failure the matter is
    blocked (followup reason) and False is returned."""
    exists_next = session.scalar(
        select(Round.id).where(Round.matter_id == matter.id,
                               Round.round_number == rnd.round_number + 1)
    )
    if exists_next is not None:
        return False
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
        return False
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
    return True


def apply_resolution_decision(session: Session, matter: Matter, llm) -> None:
    """Post-decision downstream propagation (PRD 7.4/7.5). Idempotent: every
    write is conditional or existence-guarded, safe to replay after a crash.

    approved/modified → archive (awaiting_decision → completed).
    rejected → implicit +1 credit iff at the round limit (7.5), then open the
    next round via the shared follow-up helper."""
    resolution = session.scalar(
        select(Resolution).where(Resolution.matter_id == matter.id)
        .order_by(Resolution.version.desc()).limit(1)
    )
    if resolution is None or resolution.status not in RESOLUTION_TERMINAL_STATUSES:
        return
    if resolution.status in (RESOLUTION_STATUS_APPROVED,
                             RESOLUTION_STATUS_MODIFIED):
        result = session.execute(
            update(Matter)
            .where(Matter.id == matter.id, Matter.status == "awaiting_decision")
            .values(status="completed", blocked_reason=None,
                    updated_at=utcnow())
        )
        if result.rowcount == 1:
            audit.record_audit(session, audit.MATTER_COMPLETED,
                               matter_id=matter.id,
                               detail={"resolution_id": resolution.id,
                                       "version": resolution.version,
                                       "decision": resolution.status})
        return
    # rejected → 驳回并创建新一轮（矩阵 awaiting_decision→in_progress 合法）
    source_round = session.get(Round, resolution.source_round_id)
    exists_next = session.scalar(
        select(Round.id).where(Round.matter_id == matter.id,
                               Round.round_number == source_round.round_number + 1)
    )
    if exists_next is not None:
        return
    if matter.status == "awaiting_decision":
        result = session.execute(
            update(Matter)
            .where(Matter.id == matter.id,
                   Matter.status == "awaiting_decision")
            .values(status="in_progress", updated_at=utcnow())
        )
        if result.rowcount != 1:
            return
        session.refresh(matter)
    if matter.status != "in_progress":
        return  # 异常状态（如并发取消）fail closed，不再推进
    if not can_auto_advance(
        current_round_number=source_round.round_number,
        max_rounds=matter.max_rounds,
        granted_extra_rounds=matter.granted_extra_rounds,
    ):
        granted_after = matter.granted_extra_rounds + CREDIT_GRANT_PER_CONTINUE
        session.execute(
            update(Matter)
            .where(Matter.id == matter.id)
            .values(granted_extra_rounds=granted_after, updated_at=utcnow())
        )
        session.refresh(matter)
        audit.record_audit(session, audit.MATTER_CONTINUED,
                           matter_id=matter.id,
                           detail={"mode": "reject_grant",
                                   "granted_extra_rounds": granted_after,
                                   "resolution_id": resolution.id,
                                   "rationale": (
                                       (resolution.decision_rationale or "")
                                       [:audit.AUDIT_RATIONALE_MAX] or None
                                   )})
    summary = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == source_round.id,
                                   RoundSummary.generation_status == "ok")
    )
    if summary is not None:
        _open_followup_round(session, matter, source_round, summary, llm)


def _next_resolution_version(session: Session, matter_id: str) -> int:
    current = session.scalar(
        select(func.max(Resolution.version)).where(Resolution.matter_id == matter_id)
    )
    return (current or 0) + 1


def _all_summaries_for_draft(session: Session, matter_id: str) -> list[dict]:
    """All rounds' ok summaries ordered by round_number (design §5: 草案基于
    全部轮次摘要)。"""
    rows = session.execute(
        select(Round.round_number, RoundSummary)
        .join(RoundSummary, RoundSummary.round_id == Round.id)
        .where(Round.matter_id == matter_id, RoundSummary.generation_status == "ok")
        .order_by(Round.round_number)
    ).all()
    return [
        {"round_number": round_number, **_summary_to_dict(summary)}
        for round_number, summary in rows
    ]


def _draft_resolution_phase(session: Session, matter: Matter, llm) -> None:
    """Resolution draft generation (FR-19/7.6). Idempotent: at most one draft
    per source round (source_round_id guard). converged flips the matter to
    awaiting_decision in the same transaction; provisionally_ready keeps the
    matter in_progress until the initiator chooses (PRD 7.1/7.6)."""
    if matter.status != "in_progress":
        return
    latest = session.scalar(
        select(Round).where(Round.matter_id == matter.id)
        .order_by(Round.round_number.desc()).limit(1)
    )
    if latest is None or latest.status != "closed":
        return
    summary = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == latest.id,
                                   RoundSummary.generation_status == "ok")
    )
    if summary is None or summary.convergence not in (
        CONVERGENCE_PROVISIONALLY_READY, CONVERGENCE_CONVERGED,
    ):
        return
    exists = session.scalar(
        select(Resolution.id).where(Resolution.source_round_id == latest.id)
    )
    if exists is not None:
        return
    system_prompt, user_prompt = build_resolution_draft_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
        summaries=_all_summaries_for_draft(session, matter.id),
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name="resolution_draft")
    except LLMError as e:
        _block_matter(
            session, matter,
            f"{BLOCKED_REASON_DRAFT_FAILED}：{e.error_code}"
            f"（已重试 {e.retry_count} 次）",
        )
        audit.record_audit(session, audit.LLM_FAILED, matter_id=matter.id,
                           detail={"stage": "resolution_draft",
                                   "round_id": latest.id,
                                   "error_code": e.error_code,
                                   "retry_count": e.retry_count})
        return
    resolution = Resolution(
        matter_id=matter.id, source_round_id=latest.id,
        version=_next_resolution_version(session, matter.id),
        status="pending_review",
        recommendation=data["recommendation"], rationale=data["rationale"],
        risks=data["risks"], divergences=data["divergences"],
        cited_rounds=data["cited_rounds"],
    )
    session.add(resolution)
    session.flush()
    if summary.convergence == CONVERGENCE_CONVERGED:
        session.execute(
            update(Matter)
            .where(Matter.id == matter.id, Matter.status == "in_progress")
            .values(status="awaiting_decision", blocked_reason=None,
                    updated_at=utcnow())
        )
        audit.record_audit(session, audit.MATTER_AWAITING_DECISION,
                           matter_id=matter.id,
                           detail={"resolution_id": resolution.id,
                                   "version": resolution.version,
                                   "convergence": "converged"})
    audit.record_audit(session, audit.RESOLUTION_DRAFTED, matter_id=matter.id,
                       detail={"resolution_id": resolution.id,
                               "version": resolution.version,
                               "source_round_id": latest.id,
                               "convergence": summary.convergence,
                               "cited_rounds": data["cited_rounds"]})


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
            # M3：最新轮已有决议草案（pending 或终态）说明事项停在决议闸门
            # 或已完成传播，不是分支阶段中断，不重新 tick
            has_resolution = session.scalar(
                select(Resolution.id).where(Resolution.source_round_id == latest.id)
            )
            if has_resolution is None:
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



def find_interrupted_resolution_matter_ids(session: Session) -> list[str]:
    """Startup recovery (FR-24, M3): matters whose latest resolution is
    decided but whose status never reached the post-decision state (the
    resume was lost). Re-resuming is idempotent. Matters paused at a gate
    waiting for the initiator (pending_review) are NOT returned."""
    ids: list[str] = []
    matters = list(
        session.scalars(
            select(Matter).where(Matter.status == "awaiting_decision")
        ).all()
    )
    for matter in matters:
        latest = session.scalar(
            select(Resolution)
            .where(Resolution.matter_id == matter.id)
            .order_by(Resolution.version.desc()).limit(1)
        )
        if latest is not None and latest.status in RESOLUTION_TERMINAL_STATUSES:
            ids.append(matter.id)
    return ids

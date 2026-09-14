"""Matter services: create draft, start (manual or LLM first round), queries."""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.api.pipeline import BLOCKED_REASON_DRAFT_FAILED, BLOCKED_REASON_ROUND_LIMIT
from hub.db.models import Matter, MatterParticipant, Round, Task, User
from hub.domain.credits import CREDIT_GRANT_PER_CONTINUE
from hub.domain.participants import ParticipantValidationError, validate_participants
from hub.domain.state import InvalidTransitionError, assert_matter_transition
from hub.domain.timeutil import utcnow


def create_matter(
    session: Session,
    *,
    initiator: User,
    title: str,
    goal: str,
    background: str,
    participant_ids: list[int],
    initiator_participates: bool,
    timeout_seconds: int,
    max_rounds: int,
    draft_questions: list[str],
    irreversible: bool = False,
    irreversible_reason: str | None = None,
) -> Matter:
    # 裁决 5a（2026-09-14）：勾选 irreversible 必须填写理由（重大决策留痕）
    if irreversible and not (irreversible_reason and irreversible_reason.strip()):
        raise ApiError(422, "VALIDATION_FAILED",
                       "勾选「不可逆事项」必须填写变更理由")
    try:
        validate_participants(participant_ids, initiator.id, initiator_participates)
    except ParticipantValidationError as e:
        raise ApiError(422, "PARTICIPANT_COUNT_INVALID", str(e)) from e
    unique_ids = list(dict.fromkeys(participant_ids))
    found = session.scalars(
        select(User.id).where(User.id.in_(unique_ids), User.is_active.is_(True))
    ).all()
    if len(found) != len(unique_ids):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "参与人账号不存在或未激活")
    questions = [q.strip() for q in draft_questions if q.strip()]
    if not title.strip() or not goal.strip():
        raise ApiError(422, "VALIDATION_FAILED", "主题与目标为必填项")
    matter = Matter(
        initiator_id=initiator.id,
        title=title.strip(),
        goal=goal.strip(),
        background=background,
        status="draft",
        timeout_seconds=timeout_seconds,
        max_rounds=max_rounds,
        initiator_participates=initiator_participates,
        draft_questions=questions,
    )
    matter.irreversible = irreversible
    if irreversible_reason is not None:
        matter.irreversible_reason = irreversible_reason.strip()
    session.add(matter)
    session.flush()
    for uid in unique_ids:
        session.add(MatterParticipant(matter_id=matter.id, user_id=uid))
    audit.record_audit(session, audit.MATTER_CREATED, actor_user_id=initiator.id,
                       matter_id=matter.id, detail={"participant_ids": unique_ids})
    session.flush()
    return matter


def start_matter(session: Session, *, matter_id: str, actor: User) -> Matter:
    """Start a matter: draft → in_progress (conditional UPDATE), then either
    create round 1 synchronously from manual questions (M1 path, no LLM) or
    create an empty 'generating' round for the background pipeline to fill
    (LLM path). There is never a 'collecting' matter without an open round
    (PRD 7.1); draft → collecting is never a legal transition."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED, actor_user_id=actor.id,
                           matter_id=matter_id, detail={"action": "start_matter"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可开始事项")
    try:
        assert_matter_transition(matter.status, "in_progress")
    except InvalidTransitionError:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "start_matter",
                                   "current": matter.status,
                                   "target": "in_progress"})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"当前状态 {matter.status} 不允许开始") from None
    if matter.status != "draft":
        # 矩阵允许 blocked/awaiting_decision → in_progress，但那是"继续/驳回"
        # 动作；开始动作只允许从 draft（M1 任务 16 锚定的语义保持不变）。
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "start_matter",
                                   "current": matter.status,
                                   "target": "in_progress"})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"当前状态 {matter.status} 不允许开始")
    # conditional UPDATE: only from draft
    result = session.execute(
        update(Matter)
        .where(Matter.id == matter_id, Matter.status == "draft")
        .values(status="in_progress", updated_at=utcnow())
    )
    if result.rowcount != 1:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "start_matter", "reason": "race_lost"})
        raise ApiError(409, "INVALID_STATE_TRANSITION", "事项状态已变化，请刷新后重试")
    participant_ids = session.scalars(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter.id)
    ).all()
    if matter.draft_questions:
        # 手动路径（M1 行为不变）：同步建轮建任务，matter → collecting
        round1 = Round(
            matter_id=matter.id,
            round_number=1,
            status="generating",
            questions=[
                {"question_id": f"q{i + 1}", "content": content}
                for i, content in enumerate(matter.draft_questions)
            ],
        )
        session.add(round1)
        session.flush()
        deadline = utcnow() + timedelta(seconds=matter.timeout_seconds)
        for uid in participant_ids:
            session.add(
                Task(round_id=round1.id, matter_id=matter.id, assignee_id=uid,
                     status="pending", deadline_at=deadline)
            )
        round1.status = "open"
        session.execute(
            update(Matter)
            .where(Matter.id == matter_id, Matter.status == "in_progress")
            .values(status="collecting", updated_at=utcnow())
        )
        mode = "manual"
        task_count = len(participant_ids)
    else:
        # LLM 路径：只建空 generating 轮次；出题由后台管线完成（FR-05/场景 12）。
        # 调用方（Web 路由）在 commit 后将 round1.id 入队。
        round1 = Round(
            matter_id=matter.id, round_number=1, status="generating", questions=[],
        )
        session.add(round1)
        session.flush()
        mode = "llm_generate"
        task_count = 0
    audit.record_audit(session, audit.MATTER_STARTED, actor_user_id=actor.id,
                       matter_id=matter.id,
                       detail={"round_id": round1.id, "task_count": task_count,
                               "mode": mode})
    session.flush()
    session.expire(matter)
    return session.get(Matter, matter_id)


def is_participant(session: Session, *, matter_id: str, user_id: int) -> bool:
    return session.scalar(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter_id,
            MatterParticipant.user_id == user_id,
        )
    ) is not None


def get_matter_for_user(session: Session, *, matter_id: str, user: User) -> Matter | None:
    """Initiator OR participant may see the matter (PRD 3.0). Others get None."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        return None
    if matter.initiator_id == user.id:
        return matter
    if is_participant(session, matter_id=matter_id, user_id=user.id):
        return matter
    return None


def list_matters_for_user(session: Session, *, user: User) -> list[Matter]:
    initiated_ids = session.scalars(
        select(Matter.id).where(Matter.initiator_id == user.id)
    ).all()
    participating_ids = session.scalars(
        select(MatterParticipant.matter_id).where(MatterParticipant.user_id == user.id)
    ).all()
    ids = list(dict.fromkeys([*initiated_ids, *participating_ids]))
    if not ids:
        return []
    return list(
        session.scalars(
            select(Matter).where(Matter.id.in_(ids)).order_by(Matter.created_at.desc())
        ).all()
    )


def continue_matter(session: Session, *, matter_id: str, actor: User) -> str:
    """Resume a blocked matter (PRD 7.5 / 7.1). Only the initiator. Round-limit
    blocks get +1 credit; draft-failure blocks retry the draft phase with no
    credit granted. Returns the latest round_id so the caller can enqueue a
    re-drive; the branch/draft phases are idempotent."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    session.refresh(matter)  # 避免读到 identity map 中的陈旧状态
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "continue_matter"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可继续事项")
    is_draft_retry = (matter.blocked_reason or "").startswith(
        BLOCKED_REASON_DRAFT_FAILED
    )  # 草案失败原因带 error_code/重试次数后缀，用前缀匹配
    if matter.status != "blocked" or not (
        matter.blocked_reason == BLOCKED_REASON_ROUND_LIMIT or is_draft_retry
    ):
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "continue_matter",
                                   "current": matter.status,
                                   "blocked_reason": matter.blocked_reason})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "当前状态不允许继续（仅达到轮次上限或草案生成失败的阻塞可继续）")
    assert_matter_transition(matter.status, "in_progress")
    granted_after = (
        matter.granted_extra_rounds
        if is_draft_retry
        else matter.granted_extra_rounds + CREDIT_GRANT_PER_CONTINUE
    )
    result = session.execute(
        update(Matter)
        .where(Matter.id == matter_id, Matter.status == "blocked",
               Matter.blocked_reason == matter.blocked_reason)
        .values(status="in_progress", blocked_reason=None,
                granted_extra_rounds=granted_after,
                updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "事项状态已变化，请刷新后重试")
    latest = session.scalar(
        select(Round).where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc()).limit(1)
    )
    audit.record_audit(session, audit.MATTER_CONTINUED, actor_user_id=actor.id,
                       matter_id=matter_id,
                       detail={"mode": ("retry_draft" if is_draft_retry
                                        else "round_limit"),
                               "granted_extra_rounds": granted_after,
                               "resume_round_id": latest.id})
    session.flush()
    return latest.id


CANCELLABLE_MATTER_STATUSES = frozenset(
    {"draft", "in_progress", "collecting", "awaiting_decision", "blocked"}
)


def cancel_matter(session: Session, *, matter_id: str, actor: User) -> Matter:
    """Cancel a matter (FR-08). Only the initiator; terminal/completed rejected.

    Effects: matter → ``cancelled`` (conditional UPDATE, rowcount checked);
    non-terminal tasks (pending/timeout) → ``cancelled``; open/generating
    rounds → ``closed``. Submitted/reassigned tasks are historical and stay.
    In-flight pipeline writes are conditional/idempotent and no-op against
    the cancelled state. Single transaction, caller commits."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    session.refresh(matter)  # 避免读到 identity map 中的陈旧状态
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "cancel_matter"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可取消事项")
    if matter.status not in CANCELLABLE_MATTER_STATUSES:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "cancel_matter",
                                   "current": matter.status})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"当前状态 {matter.status} 不允许取消")
    assert_matter_transition(matter.status, "cancelled")
    from_status = matter.status
    result = session.execute(
        update(Matter)
        .where(Matter.id == matter_id, Matter.status == from_status)
        .values(status="cancelled", blocked_reason=None, updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "事项状态已变化，请刷新后重试")
    # 非终态任务（pending/timeout）取消；submitted/reassigned 为历史记录，保留
    open_tasks = session.scalars(
        select(Task).join(Round, Task.round_id == Round.id)
        .where(Round.matter_id == matter_id,
               Task.status.in_(["pending", "timeout"]))
    ).all()
    for task in open_tasks:
        t_result = session.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == task.status)
            .values(status="cancelled")
        )
        if t_result.rowcount != 1:
            raise ApiError(409, "INVALID_STATE_TRANSITION",
                           "任务状态已变化，请刷新后重试")
    # 未关闭的轮次（generating/open）关闭，终止后续调度与汇总
    open_rounds = session.scalars(
        select(Round).where(Round.matter_id == matter_id,
                            Round.status.in_(["generating", "open"]))
    ).all()
    for rnd in open_rounds:
        session.execute(
            update(Round)
            .where(Round.id == rnd.id, Round.status == rnd.status)
            .values(status="closed", closed_at=utcnow())
        )
    audit.record_audit(session, audit.MATTER_CANCELLED, actor_user_id=actor.id,
                       matter_id=matter_id,
                       detail={"from_status": from_status,
                               "tasks_cancelled": len(open_tasks),
                               "rounds_closed": len(open_rounds)})
    session.flush()
    return matter


def set_agent_authority(
    session: Session,
    *,
    matter_id: str,
    user_id: int,
    agent_authority: str,
) -> MatterParticipant:
    """开通/调整某参与人在该事项上的代理授权档位（Q1/Q2 两档）。

    裁决 5b（2026-09-14）：开通限非终态事项——事项已终态
    （completed / cancelled）后决议已定型，开通 can_commit 无意义。
    """
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    if matter.status in ("completed", "cancelled"):
        audit.record_audit(
            session, audit.INVALID_STATE_TRANSITION, actor_user_id=user_id,
            matter_id=matter_id,
            detail={"action": "set_agent_authority", "current": matter.status},
        )
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "事项已终态，不再允许开通 can_commit")
    row = session.get(MatterParticipant, (matter_id, user_id))
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "该用户不是本事项参与人")
    row.agent_authority = agent_authority
    session.flush()
    return row

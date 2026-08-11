"""Matter services: create draft, start (manual or LLM first round), queries."""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.db.models import Matter, MatterParticipant, Round, Task, User
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
) -> Matter:
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

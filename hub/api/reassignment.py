"""Reassignment service (FR-08b / 6.7 / 7.1.1 / 7.3, M4 任务 2).

换人是超时与阻塞的主要出口：仅发起人可换人；原任务进入 ``reassigned`` 只读，
新任务同轮同题、新截止时间（自换人时刻按事项 timeout_seconds 重算）。
``blocked`` 换人回 ``collecting``（7.1 矩阵出口，blocked_reason 清空）。

设计决策 3（fail closed）：PRD 6.7 也列了 ``in_progress`` 可换人，但
``in_progress`` 下不存在 open 轮次与可换任务（PRD 7.1 规则），实际不可达；
本服务对 {collecting, blocked} 之外的状态一律拒绝。

原参与人的 MatterParticipant 行不移除——历史轮次已提交输出的可见性与审计链
依赖它；6.7 的可见性规则由"任务归属 + FR-07 过滤"保证，不依赖移除参与关系。

单事务，调用方 commit。
"""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.db.models import Matter, MatterParticipant, Round, Task, User
from hub.domain.timeutil import utcnow

REASSIGNABLE_MATTER_STATUSES = frozenset({"collecting", "blocked"})
REASSIGNABLE_TASK_STATUSES = frozenset({"pending", "timeout"})


def reassign_task(
    session: Session,
    *,
    matter_id: str,
    task_id: str,
    new_user_id: int,
    actor: User,
) -> Task:
    """Reassign an open-round pending/timeout task to a new participant.
    Returns the freshly created replacement task. Raises ApiError on any
    violation; all rejection paths are audited or 4xx (constraint 9)."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "reassign_task"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可换人")
    if matter.status not in REASSIGNABLE_MATTER_STATUSES:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "reassign_task",
                                   "current": matter.status})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"当前状态 {matter.status} 不允许换人")
    task = session.get(Task, task_id)
    # 任务不存在或属其他事项一律 404（不泄露他事项存在性）
    if task is None or task.matter_id != matter_id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    rnd = session.get(Round, task.round_id)
    if rnd is None or rnd.status != "open":
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "reassign_task",
                                   "reason": "round_not_open",
                                   "task_id": task_id})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "任务所属轮次未开放，不允许换人")
    if task.status not in REASSIGNABLE_TASK_STATUSES:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "reassign_task",
                                   "reason": "task_status",
                                   "task_id": task_id,
                                   "task_status": task.status})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"任务状态 {task.status} 不允许换人"
                       "（仅 pending/timeout 可换）")
    new_user = session.get(User, new_user_id)
    if new_user is None or not new_user.is_active:
        raise ApiError(422, "VALIDATION_FAILED", "新参与人账号不存在或未激活")
    if session.scalar(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter_id,
            MatterParticipant.user_id == new_user_id,
        )
    ) is not None:
        raise ApiError(422, "VALIDATION_FAILED", "新参与人不能是本事项已有参与人")

    # 原子变更区：先做所有条件 UPDATE（竞态败者直接 409，此时尚未产生任何
    # 业务变更，调用方 commit 只会落审计），再落新任务/参与人/审计。
    if matter.status == "blocked":
        # 7.1 矩阵出口 blocked→collecting；条件 UPDATE 防并发竞态
        result = session.execute(
            update(Matter)
            .where(Matter.id == matter_id, Matter.status == "blocked")
            .values(status="collecting", blocked_reason=None,
                    updated_at=utcnow())
        )
        if result.rowcount != 1:
            raise ApiError(409, "INVALID_STATE_TRANSITION",
                           "事项状态已变化，请刷新后重试")
    result = session.execute(
        update(Task)
        .where(Task.id == task_id,
               Task.status.in_(sorted(REASSIGNABLE_TASK_STATUSES)))
        .values(status="reassigned")
    )
    if result.rowcount != 1:
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "任务状态已变化，请刷新后重试")
    new_task = Task(
        round_id=task.round_id,
        matter_id=matter_id,
        assignee_id=new_user_id,
        status="pending",
        deadline_at=utcnow() + timedelta(seconds=matter.timeout_seconds),
    )
    session.add(new_task)
    session.add(MatterParticipant(matter_id=matter_id, user_id=new_user_id))
    session.flush()  # 生成 new_task.id 供审计 detail 引用
    audit.record_audit(session, audit.TASK_REASSIGNED,
                       actor_user_id=actor.id, matter_id=matter_id,
                       detail={"task_id": task_id,
                               "new_task_id": new_task.id,
                               "round_id": task.round_id,
                               "from_user_id": task.assignee_id,
                               "to_user_id": new_user_id})
    session.flush()
    return new_task

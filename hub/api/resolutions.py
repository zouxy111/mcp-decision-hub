"""Resolution decision services (FR-19~FR-22, PRD 7.4/7.5/7.6).

decide_resolution is the ONLY writer of resolution terminal states. The
version+status double-conditional UPDATE is the single concurrency 裁决点:
concurrent decisions are serialized by SQLite and the loser gets
RESOLUTION_VERSION_CONFLICT. This module never touches the graph or queues —
resume enqueue happens in the web route (task 11) and the reconciler
(task 10).
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.api.pipeline import (
    BLOCKED_REASON_DRAFT_FAILED,
    BLOCKED_REASON_ROUND_LIMIT,
    _all_summaries_for_draft,
    _next_resolution_version,
)
from hub.db.models import Matter, Resolution, Round, RoundSummary, User
from hub.domain.convergence import CONVERGENCE_PROVISIONALLY_READY
from hub.domain.resolution import (
    DECISION_MODIFIED,
    DECISION_REJECT,
    RESOLUTION_STATUS_PENDING_REVIEW,
    ResolutionValidationError,
    compose_draft_text,
    validate_decision_payload,
)
from hub.domain.state import assert_matter_transition
from hub.domain.timeutil import utcnow
from hub.llm.client import LLMError
from hub.llm.prompts import build_resolution_draft_prompt


def get_latest_resolution(
    session: Session, *, matter_id: str
) -> Resolution | None:
    return session.scalar(
        select(Resolution)
        .where(Resolution.matter_id == matter_id)
        .order_by(Resolution.version.desc())
        .limit(1)
    )


def decide_resolution(
    session: Session,
    *,
    matter_id: str,
    actor: User,
    decision: str,
    expected_version: int,
    final_text: str | None = None,
    rationale: str | None = None,
) -> Resolution:
    """Apply the initiator's decision with a version+status optimistic lock
    (FR-21b). Raises ApiError on any guard failure; never writes on
    conflict."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    session.refresh(matter)  # 避免 identity map 陈旧状态
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "decide_resolution"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可拍板")
    if matter.status != "awaiting_decision":
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "decide_resolution",
                                   "current": matter.status})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"当前状态 {matter.status} 不允许拍板")
    resolution = get_latest_resolution(session, matter_id=matter_id)
    if resolution is None:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "decide_resolution",
                                   "reason": "no_resolution"})
        raise ApiError(409, "INVALID_STATE_TRANSITION", "当前没有可拍板的决议草案")
    try:
        validate_decision_payload(
            decision=decision, final_text=final_text, rationale=rationale
        )
    except ResolutionValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e
    if decision == DECISION_MODIFIED:
        stored_final_text = final_text.strip()
    elif decision == DECISION_REJECT:
        stored_final_text = None
    else:  # approved: 平台草案即最终决议（PRD 7.4）
        stored_final_text = compose_draft_text(
            recommendation=resolution.recommendation,
            rationale=resolution.rationale,
            risks=resolution.risks,
            divergences=resolution.divergences,
        )
    result = session.execute(
        update(Resolution)
        .where(Resolution.id == resolution.id,
               Resolution.version == expected_version,
               Resolution.status == RESOLUTION_STATUS_PENDING_REVIEW)
        .values(status=decision,
                final_text=stored_final_text,
                decision_rationale=(rationale or "").strip() or None,
                decided_by=actor.id,
                decided_at=utcnow(),
                version=Resolution.version + 1)
    )
    if result.rowcount != 1:
        session.expire(resolution)
        current = session.get(Resolution, resolution.id)
        detail = {"action": "decide_resolution",
                  "expected_version": expected_version,
                  "current_version": current.version,
                  "current_status": current.status}
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail=detail)
        if current.version != expected_version:
            raise ApiError(
                409, "RESOLUTION_VERSION_CONFLICT",
                "决议版本已变化，请重新加载后再操作",
                details={"current_version": current.version,
                         "current_status": current.status},
            )
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "该决议已拍板，不可重复操作")
    audit.record_audit(session, audit.RESOLUTION_DECIDED,
                       actor_user_id=actor.id, matter_id=matter_id,
                       detail={"resolution_id": resolution.id,
                               "version": expected_version,
                               "decision": decision,
                               "rationale": (
                                   (rationale or "").strip()
                                   [:audit.AUDIT_RATIONALE_MAX] or None
                               )})
    session.flush()
    session.expire(resolution)
    return session.get(Resolution, resolution.id)


def _require_provisional_pause(session: Session, *, matter_id: str,
                               actor: User, action: str) -> tuple[Matter, Resolution]:
    """Shared guards for the two provisional-pause choices (PRD 7.6)."""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    session.refresh(matter)
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": action})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可处理决议草案")
    resolution = get_latest_resolution(session, matter_id=matter_id)
    latest_summary = None
    if resolution is not None:
        latest_summary = session.scalar(
            select(RoundSummary).where(
                RoundSummary.round_id == resolution.source_round_id,
                RoundSummary.generation_status == "ok",
            )
        )
    valid = (
        matter.status == "in_progress"
        and resolution is not None
        and resolution.status == RESOLUTION_STATUS_PENDING_REVIEW
        and latest_summary is not None
        and latest_summary.convergence == CONVERGENCE_PROVISIONALLY_READY
    )
    if not valid:
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": action,
                                   "current": matter.status,
                                   "resolution_status": (
                                       resolution.status if resolution else None
                                   )})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "当前状态不允许该操作（仅暂定收敛的草案可选择）")
    return matter, resolution


def accept_provisional(session: Session, *, matter_id: str,
                       actor: User) -> None:
    """进入拍板：in_progress → awaiting_decision（PRD 7.1/7.6）。图节点的
    resume 路径会做同样的条件 UPDATE 兜底（幂等）。"""
    matter, resolution = _require_provisional_pause(
        session, matter_id=matter_id, actor=actor, action="accept_provisional"
    )
    result = session.execute(
        update(Matter)
        .where(Matter.id == matter_id, Matter.status == "in_progress")
        .values(status="awaiting_decision", updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "事项状态已变化，请刷新后重试")
    audit.record_audit(session, audit.MATTER_AWAITING_DECISION,
                       actor_user_id=actor.id, matter_id=matter_id,
                       detail={"mode": "accept_provisional",
                               "resolution_id": resolution.id,
                               "version": resolution.version})
    session.flush()


def continue_probing(session: Session, *, matter_id: str,
                     actor: User) -> None:
    """继续追问（PRD 7.6）。只校验不改库：额度授予与新轮创建都在图的
    gate_followup 节点内幂等完成（避免 API 与节点双重授信）。"""
    _require_provisional_pause(
        session, matter_id=matter_id, actor=actor, action="continue_probing"
    )


def draft_resolution_from_blocked(session: Session, *, matter_id: str,
                                  actor: User, llm) -> Resolution:
    """PRD 7.5 "直接要求生成决议草案"：仅发起人、仅轮次上限 blocked 可触发。
    成功：pending_review 草案落库 + matter blocked→awaiting_decision。
    失败（LLM 重试耗尽）：保持 blocked（blocked_reason 不变，重试入口不受
    影响），llm_failed 审计 + 503 SERVICE_UNAVAILABLE（错误码/重试次数随
    message 渲染回详情页）。幂等：成功后 matter 已非 blocked，重复触发被
    守卫 409；并发双击由 source_round_id 唯一约束兜底。"""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    session.refresh(matter)  # 避免 identity map 陈旧状态
    if matter.initiator_id != actor.id:
        audit.record_audit(session, audit.FORBIDDEN_DENIED,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "draft_resolution_from_blocked"})
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发起人可要求生成决议草案")
    if matter.status != "blocked" or not (matter.blocked_reason or "").startswith(
        BLOCKED_REASON_ROUND_LIMIT
    ):
        audit.record_audit(session, audit.INVALID_STATE_TRANSITION,
                           actor_user_id=actor.id, matter_id=matter_id,
                           detail={"action": "draft_resolution_from_blocked",
                                   "current": matter.status,
                                   "blocked_reason": matter.blocked_reason})
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "仅达到轮次上限的阻塞可直接生成决议草案")
    latest = session.scalar(
        select(Round).where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc()).limit(1)
    )
    system_prompt, user_prompt = build_resolution_draft_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
        summaries=_all_summaries_for_draft(session, matter_id),
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name="resolution_draft")
    except LLMError as e:
        audit.record_audit(session, audit.LLM_FAILED, matter_id=matter_id,
                           detail={"stage": "resolution_draft",
                                   "trigger": "manual_from_blocked",
                                   "error_code": e.error_code,
                                   "retry_count": e.retry_count})
        raise ApiError(
            503, "SERVICE_UNAVAILABLE",
            f"{BLOCKED_REASON_DRAFT_FAILED}：{e.error_code}"
            f"（已重试 {e.retry_count} 次）",
        ) from e
    resolution = Resolution(
        matter_id=matter.id, source_round_id=latest.id,
        version=_next_resolution_version(session, matter_id),
        status="pending_review",
        recommendation=data["recommendation"], rationale=data["rationale"],
        risks=data["risks"], divergences=data["divergences"],
        cited_rounds=data["cited_rounds"],
    )
    session.add(resolution)
    session.flush()
    assert_matter_transition("blocked", "awaiting_decision")  # PRD 7.5 矩阵扩展
    result = session.execute(
        update(Matter)
        .where(Matter.id == matter.id, Matter.status == "blocked")
        .values(status="awaiting_decision", blocked_reason=None,
                updated_at=utcnow())
    )
    if result.rowcount != 1:
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "事项状态已变化，请刷新后重试")
    audit.record_audit(session, audit.RESOLUTION_DRAFTED, matter_id=matter.id,
                       detail={"resolution_id": resolution.id,
                               "version": resolution.version,
                               "source_round_id": latest.id,
                               "trigger": "manual_from_blocked",
                               "cited_rounds": data["cited_rounds"]})
    audit.record_audit(session, audit.MATTER_AWAITING_DECISION,
                       matter_id=matter.id,
                       detail={"resolution_id": resolution.id,
                               "version": resolution.version,
                               "mode": "manual_from_blocked"})
    session.flush()
    return resolution

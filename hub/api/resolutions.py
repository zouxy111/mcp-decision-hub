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
from hub.db.models import Matter, Resolution, User
from hub.domain.resolution import (
    DECISION_MODIFIED,
    DECISION_REJECT,
    RESOLUTION_STATUS_PENDING_REVIEW,
    ResolutionValidationError,
    compose_draft_text,
    validate_decision_payload,
)
from hub.domain.timeutil import utcnow


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

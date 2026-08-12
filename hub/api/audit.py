"""Audit trail writer. detail must never contain secrets, tokens or output bodies."""

from sqlalchemy.orm import Session

from hub.db.models import AuditEvent

LOGIN_SUCCESS = "login_success"
LOGIN_FAILED = "login_failed"
PASSWORD_CHANGED = "password_changed"
INVITE_CREATED = "invite_created"
INVITE_CONSUMED = "invite_consumed"
INVITE_REVOKED = "invite_revoked"
TOKEN_ISSUED = "token_issued"
TOKEN_REVOKED = "token_revoked"
MATTER_CREATED = "matter_created"
MATTER_STARTED = "matter_started"
TASK_SUBMITTED = "task_submitted"
OUTPUT_REPLAYED = "output_replayed"
INVALID_STATE_TRANSITION = "invalid_state_transition"
FORBIDDEN_DENIED = "forbidden_denied"
ROUND_SUMMARIZED = "round_summarized"
CONVERGENCE_DECIDED = "convergence_decided"
ROUND_GENERATED = "round_generated"
MATTER_BLOCKED = "matter_blocked"
MATTER_CONTINUED = "matter_continued"
LLM_FAILED = "llm_failed"
RESOLUTION_DRAFTED = "resolution_drafted"
RESOLUTION_DECIDED = "resolution_decided"
MATTER_COMPLETED = "matter_completed"
MATTER_AWAITING_DECISION = "matter_awaiting_decision"

# Max chars of a decision rationale stored in audit detail (design decision 12).
AUDIT_RATIONALE_MAX = 500


def record_audit(
    session: Session,
    event_type: str,
    *,
    actor_user_id: int | None = None,
    matter_id: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_type=event_type,
            matter_id=matter_id,
            detail=detail,
        )
    )
    session.flush()

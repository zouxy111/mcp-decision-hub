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

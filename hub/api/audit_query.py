"""Read-only audit query (FR-23b). No update/delete paths exist anywhere —
audit is append-only by construction."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.db.models import AuditEvent

DEFAULT_AUDIT_PAGE_SIZE = 50
MATTER_AUDIT_MAX = 200
DETAIL_AUDIT_PREVIEW = 20


def query_audit_events(
    session: Session,
    *,
    actor_user_id: int | None = None,
    matter_id: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_AUDIT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[AuditEvent], bool]:
    """Newest first. Returns (rows, has_more) via limit+1 probing."""
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
    if actor_user_id is not None:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
    if matter_id:
        stmt = stmt.where(AuditEvent.matter_id == matter_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.created_at < until)
    rows = list(session.scalars(stmt.offset(offset).limit(limit + 1)).all())
    return rows[:limit], len(rows) > limit

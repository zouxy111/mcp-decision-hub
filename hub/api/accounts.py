"""Account services: admin seeding, invitations, authentication, password change."""

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.passwords import hash_password, sha256_hex, verify_password
from hub.config import Settings
from hub.db.models import User
from hub.domain.timeutil import utcnow

INVITED_PASSWORD_MARKER = "!invited"


class InvitationError(ValueError):
    """Invitation credential invalid/expired/consumed. Message must not leak existence."""


def seed_admin(session: Session, settings: Settings) -> User | None:
    """Create the first admin from env config. Idempotent; no-op without env or if an
    admin already exists."""
    if not (settings.admin_username and settings.admin_initial_password):
        return None
    existing = session.scalar(select(User).where(User.is_admin.is_(True)).limit(1))
    if existing is not None:
        return None
    admin = User(
        username=settings.admin_username,
        email=f"{settings.admin_username}@local",
        password_hash=hash_password(settings.admin_initial_password),
        is_admin=True,
        is_active=True,
        must_change_password=True,
    )
    session.add(admin)
    session.flush()
    return admin


def create_invitation(
    session: Session, *, admin: User, username: str, email: str, ttl_seconds: int
) -> tuple[User, str]:
    """Create or re-send an invitation. Returns (user, plaintext_token) — the token is
    returned exactly once; only its SHA-256 is stored."""
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            username=username,
            email=email,
            password_hash=INVITED_PASSWORD_MARKER,
            is_admin=False,
            is_active=False,
            must_change_password=False,
        )
        session.add(user)
        session.flush()
    token = secrets.token_urlsafe(32)
    user.invitation_token_hash = sha256_hex(token)
    user.invitation_expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    session.flush()
    audit.record_audit(
        session, audit.INVITE_CREATED, actor_user_id=admin.id,
        detail={"target_user_id": user.id},
    )
    return user, token


def revoke_invitation(session: Session, *, admin: User, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        from hub.api.errors import ApiError

        raise ApiError(404, "RESOURCE_NOT_FOUND", "账号不存在")
    user.invitation_token_hash = None
    user.invitation_expires_at = None
    if user.password_hash == INVITED_PASSWORD_MARKER:
        user.is_active = False
    session.flush()
    audit.record_audit(
        session, audit.INVITE_REVOKED, actor_user_id=admin.id,
        detail={"target_user_id": user.id},
    )
    return user


def consume_invitation(
    session: Session, *, username: str, token: str, new_password: str
) -> User:
    user = session.scalar(select(User).where(User.username == username))
    ok = (
        user is not None
        and user.invitation_token_hash is not None
        and user.invitation_expires_at is not None
        and user.invitation_expires_at >= utcnow()
        and secrets.compare_digest(user.invitation_token_hash, sha256_hex(token))
    )
    if not ok:
        audit.record_audit(session, audit.LOGIN_FAILED,
                           detail={"reason": "invalid_invitation"})
        raise InvitationError("邀请凭证无效或已过期")
    user.password_hash = hash_password(new_password)
    user.is_active = True
    user.must_change_password = False
    user.invitation_token_hash = None
    user.invitation_expires_at = None
    session.flush()
    audit.record_audit(session, audit.INVITE_CONSUMED, actor_user_id=user.id)
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if user.password_hash == INVITED_PASSWORD_MARKER:
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user


def change_password(session: Session, *, user: User, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    session.flush()
    audit.record_audit(session, audit.PASSWORD_CHANGED, actor_user_id=user.id)

from datetime import timedelta

import pytest
from sqlalchemy import select

from hub.api import accounts
from hub.api.accounts import (
    InvitationError,
    authenticate,
    change_password,
    consume_invitation,
    create_invitation,
    revoke_invitation,
)
from hub.db.models import AuditEvent
from hub.domain.timeutil import utcnow
from tests.conftest import make_user


def test_seed_admin_creates_admin_with_forced_password_change(db_session, settings):
    settings = type(settings)(
        **{**settings.__dict__, "admin_username": "root",
           "admin_initial_password": "init-pw-123"}
    )
    admin = accounts.seed_admin(db_session, settings)
    assert admin is not None
    assert admin.is_admin is True
    assert admin.must_change_password is True
    assert authenticate(db_session, "root", "init-pw-123") is not None
    # idempotent: second call creates nothing
    assert accounts.seed_admin(db_session, settings) is None


def test_seed_admin_skipped_without_env(db_session, settings):
    assert accounts.seed_admin(db_session, settings) is None


def test_invitation_create_and_consume(db_session):
    admin = make_user(db_session, "admin", is_admin=True)
    user, token = create_invitation(
        db_session, admin=admin, username="alice", email="a@x.com", ttl_seconds=3600
    )
    assert token  # plaintext shown once
    assert user.is_active is False
    assert user.invitation_token_hash != token  # only hash stored
    assert user.invitation_expires_at > utcnow()

    consumed = consume_invitation(
        db_session, username="alice", token=token, new_password="new-pw-123"
    )
    assert consumed.is_active is True
    assert consumed.invitation_token_hash is None
    assert authenticate(db_session, "alice", "new-pw-123") is not None
    # one-time: second consume fails
    with pytest.raises(InvitationError):
        consume_invitation(db_session, username="alice", token=token,
                           new_password="other-pw-1")


def test_invitation_generic_error_does_not_leak(db_session):
    make_user(db_session, "admin", is_admin=True)
    with pytest.raises(InvitationError, match="邀请凭证无效或已过期"):
        consume_invitation(db_session, username="ghost", token="nope",
                           new_password="pw-123456")


def test_invitation_expired_rejected(db_session):
    admin = make_user(db_session, "admin", is_admin=True)
    user, token = create_invitation(
        db_session, admin=admin, username="bob", email="b@x.com", ttl_seconds=3600
    )
    user.invitation_expires_at = utcnow() - timedelta(seconds=1)
    db_session.flush()
    with pytest.raises(InvitationError):
        consume_invitation(db_session, username="bob", token=token,
                           new_password="pw-123456")


def test_reinvite_is_resend(db_session):
    admin = make_user(db_session, "admin", is_admin=True)
    user1, token1 = create_invitation(
        db_session, admin=admin, username="carol", email="c@x.com", ttl_seconds=3600
    )
    user2, token2 = create_invitation(
        db_session, admin=admin, username="carol", email="c@x.com", ttl_seconds=3600
    )
    assert user1.id == user2.id  # no duplicate account
    assert token1 != token2
    with pytest.raises(InvitationError):  # old credential invalidated
        consume_invitation(db_session, username="carol", token=token1,
                           new_password="pw-123456")
    consumed = consume_invitation(db_session, username="carol", token=token2,
                                  new_password="pw-123456")
    assert consumed.is_active is True


def test_revoke_invitation_blocks_consume(db_session):
    admin = make_user(db_session, "admin", is_admin=True)
    user, token = create_invitation(
        db_session, admin=admin, username="dave", email="d@x.com", ttl_seconds=3600
    )
    revoke_invitation(db_session, admin=admin, user_id=user.id)
    with pytest.raises(InvitationError):
        consume_invitation(db_session, username="dave", token=token,
                           new_password="pw-123456")


def test_invite_events_audited(db_session):
    admin = make_user(db_session, "admin", is_admin=True)
    user, token = create_invitation(
        db_session, admin=admin, username="erin", email="e@x.com", ttl_seconds=3600
    )
    consume_invitation(db_session, username="erin", token=token,
                       new_password="pw-123456")
    revoke_target, _ = create_invitation(
        db_session, admin=admin, username="fred", email="f@x.com", ttl_seconds=3600
    )
    revoke_invitation(db_session, admin=admin, user_id=revoke_target.id)
    types = [
        row.event_type
        for row in db_session.scalars(
            select(AuditEvent).order_by(AuditEvent.id)
        ).all()
    ]
    assert "invite_created" in types
    assert "invite_consumed" in types
    assert "invite_revoked" in types


def test_authenticate_rejects_inactive_and_wrong_password(db_session):
    make_user(db_session, "gina", password="right-pw-1", is_active=False)
    assert authenticate(db_session, "gina", "right-pw-1") is None
    make_user(db_session, "hank", password="right-pw-1")
    assert authenticate(db_session, "hank", "wrong-pw-1") is None


def test_change_password_clears_forced_flag(db_session):
    user = make_user(db_session, "ivan", password="old-pw-123", must_change_password=True)
    change_password(db_session, user=user, new_password="new-pw-456")
    assert user.must_change_password is False
    assert authenticate(db_session, "ivan", "new-pw-456") is not None
    assert authenticate(db_session, "ivan", "old-pw-123") is None

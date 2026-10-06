import pytest
from sqlalchemy import select

from hub.api import tokens as token_svc
from hub.api.errors import ApiError
from hub.db.models import AuditEvent
from tests.conftest import make_user


def test_issue_token_returns_plaintext_once(db_session):
    user = make_user(db_session, "alice")
    token, plaintext = token_svc.issue_token(db_session, user=user, name="laptop-agent")
    assert plaintext.startswith("hdt_")
    assert token.token_hash != plaintext
    assert len(token.token_hash) == 64
    assert token.revoked_at is None


def test_find_user_by_token_and_last_used(db_session):
    user = make_user(db_session, "alice")
    token, plaintext = token_svc.issue_token(db_session, user=user, name="a1")
    found = token_svc.find_user_by_token(db_session, plaintext)
    assert found is not None and found.id == user.id
    db_session.refresh(token)
    assert token.last_used_at is not None


def test_find_user_by_token_rejects_unknown(db_session):
    make_user(db_session, "alice")
    assert token_svc.find_user_by_token(db_session, "hdt_nonexistent") is None


def test_revoked_token_rejected(db_session):
    user = make_user(db_session, "alice")
    token, plaintext = token_svc.issue_token(db_session, user=user, name="a1")
    token_svc.revoke_token(db_session, user=user, token_id=token.id)
    assert token_svc.find_user_by_token(db_session, plaintext) is None


def test_revoke_other_users_token_404(db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    token, _ = token_svc.issue_token(db_session, user=alice, name="a1")
    with pytest.raises(ApiError) as exc:
        token_svc.revoke_token(db_session, user=bob, token_id=token.id)
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_list_tokens_excludes_revoked(db_session):
    user = make_user(db_session, "alice")
    t1, _ = token_svc.issue_token(db_session, user=user, name="a1")
    token_svc.issue_token(db_session, user=user, name="a2")
    token_svc.revoke_token(db_session, user=user, token_id=t1.id)
    names = [t.name for t in token_svc.list_tokens(db_session, user=user)]
    assert names == ["a2"]


def test_token_events_audited(db_session):
    user = make_user(db_session, "alice")
    token, _ = token_svc.issue_token(db_session, user=user, name="a1")
    token_svc.revoke_token(db_session, user=user, token_id=token.id)
    types = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert types == ["token_issued", "token_revoked"]
    # audit detail must not contain the token hash or plaintext
    row = db_session.scalars(select(AuditEvent)).first()
    assert "token_hash" not in (row.detail or {})


def test_last_used_at_is_throttled(db_session):
    """``last_used_at`` 有 60 秒写回窗口（读请求不为此多写一次库）。

    这是「写入提速」的一项：每个带令牌的请求都会走 resolve_user_and_token，
    每次都写就是每个读请求都多一次写事务。
    """
    from datetime import timedelta

    user = make_user(db_session, "alice")
    token, plaintext = token_svc.issue_token(db_session, user=user, name="a1")
    db_session.commit()

    token_svc.find_user_by_token(db_session, plaintext)
    db_session.commit()
    db_session.refresh(token)
    first = token.last_used_at
    assert first is not None

    # 60 秒内再调用：不重写
    token_svc.find_user_by_token(db_session, plaintext)
    db_session.commit()
    db_session.refresh(token)
    assert token.last_used_at == first

    # 超过窗口：写回新时间
    token.last_used_at = first - timedelta(seconds=120)
    db_session.commit()
    token_svc.find_user_by_token(db_session, plaintext)
    db_session.commit()
    db_session.refresh(token)
    assert token.last_used_at > first - timedelta(seconds=119)

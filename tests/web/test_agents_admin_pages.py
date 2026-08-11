import pytest
from sqlalchemy import select

from hub.db.models import AgentToken, User
from tests.conftest import make_user


def _login(client, username, password="pw-123456"):
    client.post("/login", data={"username": username, "password": password},
                follow_redirects=False)


@pytest.fixture()
def plain_user(db_session):
    user = make_user(db_session, "alice", password="pw-123456")
    db_session.commit()
    return user


def test_agents_page_empty_state(client, plain_user):
    _login(client, "alice")
    resp = client.get("/settings/agents")
    assert resp.status_code == 200
    assert "暂无 Token" in resp.text


def test_create_token_shows_plaintext_once(client, plain_user, db_session):
    _login(client, "alice")
    resp = client.post("/settings/agents", data={"name": "laptop"})
    assert resp.status_code == 200
    token = db_session.scalar(select(AgentToken))
    assert token.name == "laptop"
    assert token.token_hash not in resp.text  # only the plaintext, never the hash
    assert "hdt_" in resp.text  # plaintext shown once in the success banner
    assert "此 Token 仅显示一次" in resp.text


def test_revoke_token_via_page(client, plain_user, db_session):
    from hub.api.tokens import issue_token

    token, _ = issue_token(db_session, user=plain_user, name="old")
    db_session.commit()
    _login(client, "alice")
    resp = client.post(f"/settings/agents/{token.id}/revoke",
                       follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(AgentToken, token.id).revoked_at is not None


def test_admin_invitations_requires_admin(client, plain_user):
    _login(client, "alice")
    resp = client.get("/admin/invitations")
    assert resp.status_code == 403


def test_admin_create_invitation_shows_credential(client, db_session):
    make_user(db_session, "root", password="pw-123456", is_admin=True)
    db_session.commit()
    _login(client, "root")
    resp = client.post("/admin/invitations",
                       data={"username": "newbie", "email": "n@x.com"})
    assert resp.status_code == 200
    assert "仅显示一次" in resp.text
    invited = db_session.scalar(select(User).where(User.username == "newbie"))
    assert invited is not None
    assert invited.is_active is False
    assert invited.invitation_token_hash not in resp.text


def test_admin_revoke_invitation(client, db_session):
    from hub.api.accounts import create_invitation

    admin = make_user(db_session, "root", password="pw-123456", is_admin=True)
    invited, _ = create_invitation(db_session, admin=admin, username="nb",
                                   email="nb@x.com", ttl_seconds=3600)
    db_session.commit()
    _login(client, "root")
    resp = client.post(f"/admin/invitations/{invited.id}/revoke",
                       follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(User, invited.id).invitation_token_hash is None

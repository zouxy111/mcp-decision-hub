"""Login/invite rate limiting tests (P1 登录接口限流).

Failure-counting semantics: only failed attempts consume the budget; peek
blocks early with HTTP 429 once a dimension (username / IP) is over limit.
"""

import pytest
from sqlalchemy import select

from hub.db.models import AuditEvent
from tests.conftest import make_user


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


@pytest.fixture()
def rl_settings(tmp_path):
    from hub.config import Settings
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        session_secret="test-secret",
        admin_username=None,
        admin_initial_password=None,
        llm_provider_name="DeepSeek（测试）",
        rate_limit_login_username_per_minute=2,
        rate_limit_login_ip_per_minute=100,
    )


def test_username_dimension_blocks_after_failures(rl_settings, db_session):
    """2 failures allowed, 3rd attempt throttled (even with correct password)."""
    from fastapi.testclient import TestClient

    from hub.main import create_app

    make_user(db_session, "victim", password="right-pw-123")
    db_session.commit()
    with TestClient(create_app(rl_settings)) as client:
        r1 = _login(client, "victim", "wrong-1")
        r2 = _login(client, "victim", "wrong-2")
        assert r1.status_code == 200 and "用户名或密码错误" in r1.text
        assert r2.status_code == 200
        # 3rd attempt: throttled before authenticate — correct password rejected too
        r3 = _login(client, "victim", "right-pw-123")
        assert r3.status_code == 429
        assert "尝试过于频繁" in r3.text


def test_successful_login_does_not_consume_budget(rl_settings, db_session):
    from fastapi.testclient import TestClient

    from hub.main import create_app

    make_user(db_session, "alice", password="pw-123456")
    db_session.commit()
    with TestClient(create_app(rl_settings)) as client:
        for _ in range(4):
            resp = _login(client, "alice", "pw-123456")
            assert resp.status_code == 303  # never throttled


def test_rate_limited_audit_written(rl_settings, db_session):
    from fastapi.testclient import TestClient

    from hub.main import create_app

    with TestClient(create_app(rl_settings)) as client:
        _login(client, "nobody", "x")
        _login(client, "nobody", "x")
        _login(client, "nobody", "x")
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "login_rate_limited")
    ).all()
    assert len(events) == 1


@pytest.fixture()
def ip_settings(tmp_path):
    from hub.config import Settings
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        session_secret="test-secret",
        admin_username=None,
        admin_initial_password=None,
        llm_provider_name="DeepSeek（测试）",
        rate_limit_login_username_per_minute=100,
        rate_limit_login_ip_per_minute=2,
    )


def test_ip_dimension_shared_across_usernames(ip_settings, db_session):
    from fastapi.testclient import TestClient

    from hub.main import create_app

    with TestClient(create_app(ip_settings)) as client:
        assert _login(client, "user_a", "x").status_code == 200
        assert _login(client, "user_b", "x").status_code == 200
        # same client IP exhausted: any username throttled
        assert _login(client, "user_c", "x").status_code == 429


def test_invite_consume_throttled(rl_settings, db_session):
    from fastapi.testclient import TestClient

    from hub.main import create_app

    with TestClient(create_app(rl_settings)) as client:
        for i in range(2):
            resp = client.post("/invite/consume", data={
                "username": "invited", "invitation_token": f"bad-{i}",
                "new_password": "pw-12345678"}, follow_redirects=False)
            assert resp.status_code == 200
            assert "邀请凭证无效" in resp.text
        resp = client.post("/invite/consume", data={
            "username": "invited", "invitation_token": "bad-3",
            "new_password": "pw-12345678"}, follow_redirects=False)
        assert resp.status_code == 429
        assert "尝试过于频繁" in resp.text

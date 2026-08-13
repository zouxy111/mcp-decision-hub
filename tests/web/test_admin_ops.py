"""/admin/ops page tests (PRD 10.4, M4 任务 8)."""

import pytest

from tests.conftest import make_user


@pytest.fixture()
def admin(db_session):
    u = make_user(db_session, "admin", is_admin=True, password="pw-123456")
    db_session.commit()
    return u


def _login_admin(client):
    client.post("/login", data={"username": "admin", "password": "pw-123456"},
                follow_redirects=False)


def test_admin_ops_page_loads(client, db_session, admin):
    _login_admin(client)
    resp = client.get("/admin/ops")
    assert resp.status_code == 200
    assert "调度器状态" in resp.text
    assert "drive_queue" in resp.text
    assert "resume_queue" in resp.text
    assert "task_timeout" in resp.text or "暂无超时记录" in resp.text
    assert "task_reassigned" in resp.text or "暂无换人记录" in resp.text


def test_non_admin_redirected(client, db_session):
    make_user(db_session, "regular", password="pw-123456")
    db_session.commit()
    client.post("/login", data={"username": "regular", "password": "pw-123456"},
                follow_redirects=False)
    resp = client.get("/admin/ops", follow_redirects=False)
    # require_admin redirects non-admins to login
    assert resp.status_code in (302, 303, 403)

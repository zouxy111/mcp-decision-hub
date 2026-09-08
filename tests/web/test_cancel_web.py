"""Cancel matter web layer tests (FR-08, P1)."""

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Matter, Task
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    outsider = make_user(db_session, "outsider", password="pw-123456")
    db_session.commit()
    return init, alice, bob, outsider


@pytest.fixture()
def matter(db_session, users):
    init, alice, bob, _ = users
    m = matter_svc.create_matter(
        db_session, initiator=init, title="选型", goal="定方案", background="背景",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=m.id, actor=init)
    db_session.commit()
    return m


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def test_initiator_sees_cancel_button(client, db_session, users, matter):
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "取消事项" in resp.text


def test_participant_no_cancel_button(client, db_session, users, matter):
    _login(client, "alice")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "取消事项" not in resp.text


def test_cancel_flow(client, db_session, users, matter):
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()  # 清 identity map 陈旧读（Core UPDATE 不回流 ORM 对象）
    assert db_session.get(Matter, matter.id).status == "cancelled"
    tasks = db_session.scalars(select(Task).where(Task.matter_id == matter.id)).all()
    assert all(t.status == "cancelled" for t in tasks)
    # 详情页不再出现取消按钮
    resp = client.get(f"/matters/{matter.id}")
    assert "取消事项" not in resp.text
    assert "cancelled" in resp.text


def test_participant_cancel_forbidden(client, db_session, users, matter):
    _login(client, "alice")
    resp = client.post(f"/matters/{matter.id}/cancel", follow_redirects=False)
    assert resp.status_code == 403
    assert db_session.get(Matter, matter.id).status == "collecting"


def test_outsider_cancel_not_found(client, db_session, users, matter):
    _login(client, "outsider")
    resp = client.post(f"/matters/{matter.id}/cancel", follow_redirects=False)
    assert resp.status_code == 404


def test_completed_matter_no_cancel_button(client, db_session, users, matter):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="completed")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "取消事项" not in resp.text
    # 直接 POST 也被拒绝
    resp = client.post(f"/matters/{matter.id}/cancel", follow_redirects=False)
    assert resp.status_code == 409

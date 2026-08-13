"""Reassignment web layer tests (FR-08b, M4 任务 2)."""

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Task
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    carol = make_user(db_session, "carol", password="pw-123456")
    db_session.commit()
    return init, alice, bob, carol


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


def _task_for(db_session, matter, assignee):
    return db_session.scalar(
        select(Task).where(Task.matter_id == matter.id,
                           Task.assignee_id == assignee.id))


def test_initiator_detail_shows_reassign_form(client, db_session, users, matter):
    init, alice, bob, carol = users
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "换人" in resp.text
    assert "alice" in resp.text and "bob" in resp.text  # reassignable tasks
    # carol is an available candidate, not alice/bob (already participants)
    assert "carol" in resp.text


def test_participant_detail_has_no_reassign_form(client, db_session, users, matter):
    _login(client, "alice")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "换人" not in resp.text


def test_post_reassign_redirects_and_shows_new_task(
    client, db_session, users, matter
):
    init, alice, bob, carol = users
    task_b = _task_for(db_session, matter, bob)
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/reassign",
                       data={"task_id": task_b.id, "new_user_id": carol.id},
                       follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    # 原任务 reassigned；新任务出现给 carol
    assert db_session.get(Task, task_b.id).status == "reassigned"
    new_task = db_session.scalar(
        select(Task).where(Task.matter_id == matter.id,
                           Task.assignee_id == carol.id,
                           Task.status == "pending"))
    assert new_task is not None
    # 详情页反映新状态
    detail = client.get(f"/matters/{matter.id}")
    assert "reassigned" in detail.text
    assert "carol" in detail.text


def test_post_reassign_invalid_task_renders_error(client, db_session, users, matter):
    init, alice, bob, carol = users
    task_b = _task_for(db_session, matter, bob)
    # 让 task_b 已 submitted（不可换）
    db_session.execute(
        update(Task).where(Task.id == task_b.id).values(status="submitted")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/reassign",
                       data={"task_id": task_b.id, "new_user_id": carol.id},
                       follow_redirects=False)
    assert resp.status_code == 409
    assert "不允许换人" in resp.text


def test_participant_post_reassign_forbidden(client, db_session, users, matter):
    _, alice, bob, carol = users
    task_b = _task_for(db_session, matter, bob)
    _login(client, "alice")
    resp = client.post(f"/matters/{matter.id}/reassign",
                       data={"task_id": task_b.id, "new_user_id": carol.id},
                       follow_redirects=False)
    assert resp.status_code == 403

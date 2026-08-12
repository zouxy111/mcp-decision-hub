"""App 级超时流程测试（FR-14b / PRD 10.4，场景 13 的自动化近似）。

超时构造一律直改库设置 deadline_at，不等待真实扫描周期；扫描通过手动调
``hub.api.scheduler.scan_once`` 触发（关键实现约束 16）。重启语义测试在
DB 构造完成后才创建 TestClient（新 lifespan 重建 timeout_worker）。
"""

from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.scheduler import scan_once
from hub.db.models import AuditEvent, Matter, Task
from hub.domain.timeutil import utcnow
from tests.conftest import make_user


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def _make_users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    return init, alice, bob


def _create_and_start_manual_matter(client, db_session, alice, bob):
    """发起人经 Web 路由创建手动问题事项并开始。"""
    _login(client, "init")
    data = {
        "title": "选型决策", "goal": "定下方案", "background": "背景材料",
        "timeout_hours": "72", "questions_text": "问题一？",
        "participant_ids": [str(alice.id), str(bob.id)],
    }
    resp = client.post("/matters/new", data=data, follow_redirects=False)
    assert resp.status_code == 303
    matter = db_session.scalar(select(Matter))
    resp = client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    return db_session.get(Matter, matter.id)


def _expire_task(session_factory, task_id):
    with session_factory() as s:
        s.execute(
            update(Task).where(Task.id == task_id)
            .values(deadline_at=utcnow() - timedelta(seconds=5))
        )
        s.commit()


def _timeout_audits(db_session):
    db_session.expire_all()
    return list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "task_timeout")
    ).all())


def _scan_via_app(client):
    return scan_once(client.app.state.session_factory,
                     client.app.state.settings,
                     drive_queue=client.app.state.drive_queue)


def test_scan_once_times_out_expired_task_app_level(client, db_session,
                                                    session_factory):
    _, alice, bob = _make_users(db_session)
    matter = _create_and_start_manual_matter(client, db_session, alice, bob)
    assert matter.status == "collecting"
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    _expire_task(session_factory, task_b.id)

    processed = _scan_via_app(client)
    assert processed == 1

    db_session.expire_all()
    assert db_session.get(Task, task_b.id).status == "timeout"
    task_a_id = db_session.scalar(
        select(Task.id).where(Task.assignee_id == alice.id))
    assert db_session.get(Task, task_a_id).status == "pending"
    events = _timeout_audits(db_session)
    assert len(events) == 1
    assert events[0].detail == {"task_id": task_b.id,
                                "round_id": task_b.round_id}


def test_restart_semantics_timeout_within_one_scan(settings, session_factory,
                                                   db_session):
    """先构造过期任务，再创建 TestClient（新 lifespan 重建 timeout_worker）；
    触发一次扫描 → 任务在一个扫描周期内被判 timeout（场景 13 近似）。

    lifespan 的 timeout_worker 启动即扫一次，本测试不断言"由谁"置 timeout，
    只断言状态与审计恰好一次——这正是重启恢复的语义。"""
    # 覆盖为大周期：worker 在测试期间不会真正触发第二次扫描
    settings = replace(settings, timeout_scan_interval_seconds=3600)

    init, alice, bob = _make_users(db_session)
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="选型决策", goal="定下方案",
        background="背景材料",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["问题一？"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    _expire_task(session_factory, task_b.id)

    from hub.main import create_app

    app = create_app(settings, llm=None)
    with TestClient(app) as client:
        # 手动再触发一次扫描（幂等；若 worker 启动扫描已处理则返回 0）
        _scan_via_app(client)
        db_session.expire_all()
        assert db_session.get(Task, task_b.id).status == "timeout"
        assert db_session.get(Matter, matter.id).status in (
            "collecting", "in_progress")
    assert len(_timeout_audits(db_session)) == 1


def test_lifespan_worker_scans_expired_task_on_startup(settings,
                                                       session_factory,
                                                       db_session):
    """重启恢复（场景 13）：过期任务已存在时才创建 TestClient，lifespan
    重建的 timeout_worker 启动即扫（先扫后睡），无需手动触发、不等待完整
    周期即判定 timeout。"""
    import time

    settings = replace(settings, timeout_scan_interval_seconds=3600)
    init, alice, bob = _make_users(db_session)
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="选型决策", goal="定下方案",
        background="背景材料",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["问题一？"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    _expire_task(session_factory, task_b.id)

    from hub.main import create_app

    app = create_app(settings, llm=None)

    def status():
        with session_factory() as s:
            return s.get(Task, task_b.id).status

    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if status() == "timeout":
                break
            time.sleep(0.05)
        assert status() == "timeout"
    assert len(_timeout_audits(db_session)) == 1


def test_repeated_scan_no_duplicate_audit_app_level(client, db_session,
                                                    session_factory):
    _, alice, bob = _make_users(db_session)
    matter = _create_and_start_manual_matter(client, db_session, alice, bob)
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    _expire_task(session_factory, task_b.id)

    assert _scan_via_app(client) == 1
    assert _scan_via_app(client) == 0
    assert len(_timeout_audits(db_session)) == 1
    db_session.expire_all()
    assert db_session.get(Task, task_b.id).status == "timeout"
    assert db_session.get(Matter, matter.id).status == "collecting"

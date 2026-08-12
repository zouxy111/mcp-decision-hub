"""Timeout scan service unit tests (FR-14b / PRD 10.4, M4 任务 1).

超时构造一律直改库设置 deadline_at（过去/未来），不等待真实扫描周期
（关键实现约束 16）。
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api import scheduler
from hub.api.pipeline import maybe_drive_round
from hub.api.scheduler import SCHEDULER_STATE, scan_once
from hub.background import _scan_safe
from hub.db.models import AuditEvent, Matter, Round, Task
from hub.domain.collection import count_submitted
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from hub.mcp_server.methods import mcp_submit_output
from tests.conftest import make_user


class _QueueStub:
    """drive_queue 替身：只收集 put_nowait 的条目。"""

    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    SCHEDULER_STATE.last_run_at = None
    SCHEDULER_STATE.last_processed = 0
    SCHEDULER_STATE.last_duration_ms = 0
    SCHEDULER_STATE.consecutive_failures = 0
    SCHEDULER_STATE.total_timeouts = 0


@pytest.fixture()
def scenario(db_session):
    """手动问题路径：start 后 matter collecting、round open、两个 pending 任务。"""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    tasks = {
        t.assignee_id: t
        for t in db_session.scalars(select(Task)).all()
    }
    rnd = db_session.scalar(select(Round))
    assert rnd.status == "open"
    return {"init": init, "alice": alice, "bob": bob, "matter": matter,
            "round": rnd, "task_a": tasks[alice.id], "task_b": tasks[bob.id]}


def _expire(db_session, task_id):
    db_session.execute(
        update(Task).where(Task.id == task_id)
        .values(deadline_at=utcnow() - timedelta(seconds=10))
    )
    db_session.commit()


def _timeout_audits(db_session):
    return list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "task_timeout")
    ).all())


def _submit_payload(task_id):
    answers = [{"question_id": "q1", "content": "回答一"}]
    return {
        "task_id": task_id,
        "answers": answers,
        "notes": "备注",
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, "备注"),
        "idempotency_key": str(uuid.uuid4()),
    }


def test_scan_once_times_out_expired_pending_task(db_session, session_factory,
                                                  settings, scenario):
    _expire(db_session, scenario["task_b"].id)
    processed = scan_once(session_factory, settings)
    assert processed == 1
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_b"].id).status == "timeout"
    assert db_session.get(Task, scenario["task_a"].id).status == "pending"
    events = _timeout_audits(db_session)
    assert len(events) == 1
    event = events[0]
    assert event.actor_user_id is None  # 系统动作
    assert event.matter_id == scenario["matter"].id
    assert event.detail == {"task_id": scenario["task_b"].id,
                            "round_id": scenario["round"].id}


def test_scan_once_ignores_future_deadline_and_non_pending(
    db_session, session_factory, settings, scenario
):
    # task_b 保持未来截止（默认 +3600s）；task_a 直改为 cancelled
    db_session.execute(
        update(Task).where(Task.id == scenario["task_a"].id)
        .values(status="cancelled")
    )
    db_session.execute(
        update(Task).where(Task.id == scenario["task_a"].id)
        .values(deadline_at=utcnow() - timedelta(seconds=10))
    )
    db_session.commit()
    processed = scan_once(session_factory, settings)
    assert processed == 0
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_a"].id).status == "cancelled"
    assert db_session.get(Task, scenario["task_b"].id).status == "pending"
    assert _timeout_audits(db_session) == []


def test_scan_once_ignores_submitted_task_past_deadline(
    db_session, session_factory, settings, scenario
):
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["alice"].id,
                               payload=_submit_payload(scenario["task_a"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    _expire(db_session, scenario["task_a"].id)
    processed = scan_once(session_factory, settings)
    assert processed == 0
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_a"].id).status == "submitted"
    assert _timeout_audits(db_session) == []


def test_scan_once_is_idempotent(db_session, session_factory, settings, scenario):
    _expire(db_session, scenario["task_b"].id)
    assert scan_once(session_factory, settings) == 1
    assert scan_once(session_factory, settings) == 0
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_b"].id).status == "timeout"
    assert len(_timeout_audits(db_session)) == 1


def test_scan_once_drives_partial_collection(db_session, session_factory,
                                             settings, scenario):
    """A 正常提交 + B 到期 → 收齐翻转 + round_id 入队（先 commit 后入队）。"""
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["alice"].id,
                               payload=_submit_payload(scenario["task_a"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    # 提交后轮内仍有 pending 任务 → 不应已收齐
    assert maybe_drive_round(db_session, task_id=scenario["task_a"].id) is None
    _expire(db_session, scenario["task_b"].id)
    queue = _QueueStub()
    processed = scan_once(session_factory, settings, drive_queue=queue)
    assert processed == 1
    assert queue.items == [scenario["round"].id]
    db_session.expire_all()
    assert db_session.get(Round, scenario["round"].id).status == "awaiting_summary"
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"


def test_scan_once_all_timeout_still_collects(db_session, session_factory,
                                              settings, scenario):
    """全部超时：收齐翻转成立，count_submitted == 0 事实可断言。"""
    _expire(db_session, scenario["task_a"].id)
    _expire(db_session, scenario["task_b"].id)
    queue = _QueueStub()
    processed = scan_once(session_factory, settings, drive_queue=queue)
    assert processed == 2
    assert queue.items == [scenario["round"].id]  # 去重后只入队一次
    db_session.expire_all()
    statuses = list(db_session.scalars(
        select(Task.status).where(Task.round_id == scenario["round"].id)
    ).all())
    assert count_submitted(statuses) == 0
    assert db_session.get(Round, scenario["round"].id).status == "awaiting_summary"
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"
    assert len(_timeout_audits(db_session)) == 2


def test_scan_safe_updates_state_on_success(session_factory, settings, scenario,
                                            db_session):
    _expire(db_session, scenario["task_b"].id)
    before = utcnow()
    returned = _scan_safe(session_factory, settings, None)
    assert returned == 1
    assert SCHEDULER_STATE.last_run_at is not None
    assert SCHEDULER_STATE.last_run_at >= before
    assert SCHEDULER_STATE.last_processed == 1
    assert SCHEDULER_STATE.last_duration_ms >= 0
    assert SCHEDULER_STATE.consecutive_failures == 0
    assert SCHEDULER_STATE.total_timeouts == 1


def test_scan_safe_records_failure_without_raising(session_factory, settings,
                                                   monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "scan_once", _boom)
    returned = _scan_safe(session_factory, settings, None)
    assert returned == 0
    assert SCHEDULER_STATE.consecutive_failures == 1
    assert SCHEDULER_STATE.last_run_at is not None
    assert SCHEDULER_STATE.last_processed == 0
    assert SCHEDULER_STATE.total_timeouts == 0
    _scan_safe(session_factory, settings, None)
    assert SCHEDULER_STATE.consecutive_failures == 2

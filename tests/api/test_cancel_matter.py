"""Cancel matter service tests (FR-08, P1).

构造手动问题路径（start 后 matter collecting、round open、pending 任务），
直改库制造 timeout/submitted/draft/completed 状态；不等后台、不调 LLM。
"""

import uuid

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import AuditEvent, Matter, Round, Task
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from hub.mcp_server.methods import mcp_submit_output
from tests.conftest import make_user


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
    tasks = {t.assignee_id: t for t in db_session.scalars(select(Task)).all()}
    rnd = db_session.scalar(select(Round))
    assert rnd.status == "open"
    return {"init": init, "alice": alice, "bob": bob, "matter": matter,
            "round": rnd, "task_a": tasks[alice.id], "task_b": tasks[bob.id]}


def _submit_payload(task_id):
    answers = [{"question_id": "q1", "content": "回答一"}]
    return {
        "task_id": task_id, "answers": answers, "notes": "备注",
        "human_approved": True, "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, "备注"),
        "idempotency_key": str(uuid.uuid4()),
    }


def _cancel_audits(db_session):
    return list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "matter_cancelled")
    ).all())


def test_initiator_cancels_collecting_matter(db_session, scenario):
    matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                             actor=scenario["init"])
    db_session.commit()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "cancelled"
    assert matter.blocked_reason is None
    # pending 任务全部取消
    for key in ("task_a", "task_b"):
        assert db_session.get(Task, scenario[key].id).status == "cancelled"
    # 开放轮关闭
    rnd = db_session.get(Round, scenario["round"].id)
    assert rnd.status == "closed" and rnd.closed_at is not None
    # 审计
    events = _cancel_audits(db_session)
    assert len(events) == 1
    assert events[0].detail["from_status"] == "collecting"
    assert events[0].detail["tasks_cancelled"] == 2
    assert events[0].detail["rounds_closed"] == 1


def test_non_initiator_forbidden(db_session, scenario):
    with pytest.raises(ApiError) as exc:
        matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                                 actor=scenario["alice"])
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FORBIDDEN_SCOPE"
    assert db_session.get(Matter, scenario["matter"].id).status == "collecting"
    denied = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "forbidden_denied")
    ).all()
    assert any(e.detail.get("action") == "cancel_matter" for e in denied)


def test_completed_matter_rejected(db_session, scenario):
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="completed")
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                                 actor=scenario["init"])
    assert exc.value.status_code == 409


def test_cancel_is_terminal(db_session, scenario):
    matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                             actor=scenario["init"])
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                                 actor=scenario["init"])
    assert exc.value.status_code == 409


def test_draft_matter_cancellable(db_session):
    init = make_user(db_session, "init2")
    alice = make_user(db_session, "alice2")
    bob = make_user(db_session, "bob2")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    db_session.commit()
    assert matter.status == "draft"
    matter_svc.cancel_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    assert db_session.get(Matter, matter.id).status == "cancelled"


def test_timeout_tasks_also_cancelled(db_session, scenario):
    db_session.execute(
        update(Task).where(Task.id == scenario["task_b"].id)
        .values(status="timeout")
    )
    db_session.commit()
    matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                             actor=scenario["init"])
    db_session.commit()
    assert db_session.get(Task, scenario["task_b"].id).status == "cancelled"


def test_submitted_task_preserved(db_session, settings, scenario):
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["alice"].id,
                               payload=_submit_payload(scenario["task_a"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                             actor=scenario["init"])
    db_session.commit()
    # 已提交任务为历史记录，保持 submitted；未提交的取消
    assert db_session.get(Task, scenario["task_a"].id).status == "submitted"
    assert db_session.get(Task, scenario["task_b"].id).status == "cancelled"


def test_submit_after_cancel_rejected(db_session, settings, scenario):
    matter_svc.cancel_matter(db_session, matter_id=scenario["matter"].id,
                             actor=scenario["init"])
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        mcp_submit_output(db_session, settings,
                          user_id=scenario["alice"].id,
                          payload=_submit_payload(scenario["task_a"].id))
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"

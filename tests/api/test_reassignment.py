"""Reassignment service unit tests (FR-08b / 6.7 / 7.1.1 / 7.3, M4 任务 2).

构造一律手动问题路径（start 后 matter collecting、round open、pending 任务）
+ 直改库制造 timeout/blocked 等状态；不等后台、不调 LLM。
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api import reassignment as reassign_svc
from hub.api.errors import ApiError
from hub.api.pipeline import maybe_drive_round
from hub.db.models import AuditEvent, Matter, MatterParticipant, Round, Task
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from hub.mcp_server.methods import mcp_submit_output
from tests.conftest import make_user

TIMEOUT_SECONDS = 3600


@pytest.fixture()
def scenario(db_session):
    """手动问题路径：start 后 matter collecting、round open、两个 pending 任务。"""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=TIMEOUT_SECONDS, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    tasks = {
        t.assignee_id: t
        for t in db_session.scalars(select(Task)).all()
    }
    rnd = db_session.scalar(select(Round))
    assert rnd.status == "open"
    return {"init": init, "alice": alice, "bob": bob, "carol": carol,
            "matter": matter, "round": rnd,
            "task_a": tasks[alice.id], "task_b": tasks[bob.id]}


def _reassign_audits(db_session):
    return list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "task_reassigned")
    ).all())


def _make_timeout(db_session, task):
    """直改库把任务置为 timeout（过去 deadline）。"""
    db_session.execute(
        update(Task).where(Task.id == task.id)
        .values(status="timeout",
                deadline_at=utcnow() - timedelta(seconds=10))
    )
    db_session.commit()


def _make_blocked(db_session, matter, reason="达到轮次上限"):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason=reason)
    )
    db_session.commit()


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


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_reassign_pending_task_success(db_session, scenario):
    before_round_number = scenario["round"].round_number
    before_granted = scenario["matter"].granted_extra_rounds
    new_task = reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"],
    )
    db_session.commit()
    db_session.expire_all()
    # 原任务 reassigned
    assert db_session.get(Task, scenario["task_a"].id).status == "reassigned"
    # 新任务：新 id、pending、同轮、新参与人
    assert new_task.id != scenario["task_a"].id
    fresh = db_session.get(Task, new_task.id)
    assert fresh.status == "pending"
    assert fresh.round_id == scenario["round"].id
    assert fresh.matter_id == scenario["matter"].id
    assert fresh.assignee_id == scenario["carol"].id
    # deadline_at ≈ utcnow() + timeout_seconds（±60s 容差）
    expected = utcnow() + timedelta(seconds=TIMEOUT_SECONDS)
    assert abs((fresh.deadline_at - expected).total_seconds()) <= 60
    # 新参与人进 MatterParticipant；原参与人行保留
    pids = set(db_session.scalars(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == scenario["matter"].id)
    ).all())
    assert pids == {scenario["alice"].id, scenario["bob"].id,
                    scenario["carol"].id}
    # 轮次编号与 granted_extra_rounds 不变
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    assert matter.granted_extra_rounds == before_granted
    rnd = db_session.get(Round, scenario["round"].id)
    assert rnd.round_number == before_round_number
    assert rnd.status == "open"
    # 审计 detail 五个键
    events = _reassign_audits(db_session)
    assert len(events) == 1
    e = events[0]
    assert e.actor_user_id == scenario["init"].id
    assert e.matter_id == scenario["matter"].id
    assert e.detail == {"task_id": scenario["task_a"].id,
                        "new_task_id": new_task.id,
                        "round_id": scenario["round"].id,
                        "from_user_id": scenario["alice"].id,
                        "to_user_id": scenario["carol"].id}


def test_reassign_timeout_task_success(db_session, scenario):
    _make_timeout(db_session, scenario["task_b"])
    new_task = reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_b"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"],
    )
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_b"].id).status == "reassigned"
    fresh = db_session.get(Task, new_task.id)
    assert fresh.status == "pending"
    assert fresh.round_id == scenario["round"].id
    assert fresh.assignee_id == scenario["carol"].id
    assert db_session.get(Matter, scenario["matter"].id).status == "collecting"
    assert len(_reassign_audits(db_session)) == 1


def test_reassign_flips_blocked_back_to_collecting(db_session, scenario):
    """7.1 矩阵 blocked→collecting 换人出口；blocked_reason 清空。"""
    _make_blocked(db_session, scenario["matter"], reason="本轮无有效输出")
    _make_timeout(db_session, scenario["task_a"])
    reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"],
    )
    db_session.commit()
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    assert matter.blocked_reason is None


# ---------------------------------------------------------------------------
# 拒绝路径
# ---------------------------------------------------------------------------


def test_reassign_matter_not_found(db_session, scenario):
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id="mat_missing",
            task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_reassign_non_initiator_forbidden_with_audit(db_session, scenario):
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
            actor=scenario["alice"],
        )
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FORBIDDEN_SCOPE"
    db_session.commit()
    events = list(db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == "forbidden_denied")
    ).all())
    assert len(events) == 1
    assert events[0].actor_user_id == scenario["alice"].id


@pytest.mark.parametrize("status", ["draft", "in_progress",
                                    "awaiting_decision", "completed"])
def test_reassign_rejects_non_reassignable_matter_status(db_session, scenario,
                                                          status):
    """collecting/blocked 之外的状态一律 409（in_progress 下无 open 轮次与可换
    任务，实际不可达；fail closed 拒绝）。"""
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status=status)
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"
    db_session.commit()
    events = list(db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == "invalid_state_transition")
    ).all())
    assert len(events) == 1


def test_reassign_rejects_submitted_task(db_session, settings, scenario):
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["alice"].id,
                               payload=_submit_payload(scenario["task_a"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_a"].id).status == "submitted"


def test_reassign_rejects_non_open_round(db_session, scenario):
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"
    db_session.commit()
    events = list(db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == "invalid_state_transition")
    ).all())
    assert len(events) == 1


def test_reassign_task_not_found(db_session, scenario):
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id="tsk_missing", new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_reassign_task_of_other_matter_404(db_session, scenario):
    """任务属其他事项 → 404（不泄露他事项存在性）。"""
    other = matter_svc.create_matter(
        db_session, initiator=scenario["init"], title="T2", goal="G2",
        background="B2",
        participant_ids=[scenario["alice"].id, scenario["bob"].id],
        initiator_participates=False, timeout_seconds=TIMEOUT_SECONDS,
        max_rounds=10, draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=other.id,
                            actor=scenario["init"])
    db_session.commit()
    other_task = db_session.scalar(
        select(Task).where(Task.matter_id == other.id))
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=other_task.id, new_user_id=scenario["carol"].id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("case", ["existing_participant", "missing_user",
                                  "inactive_user"])
def test_reassign_rejects_invalid_new_participant(db_session, scenario, case):
    if case == "existing_participant":
        new_user_id = scenario["bob"].id
    elif case == "missing_user":
        new_user_id = 999999
    else:
        inactive = make_user(db_session, "inactive", is_active=False)
        db_session.commit()
        new_user_id = inactive.id
    with pytest.raises(ApiError) as exc:
        reassign_svc.reassign_task(
            db_session, matter_id=scenario["matter"].id,
            task_id=scenario["task_a"].id, new_user_id=new_user_id,
            actor=scenario["init"],
        )
    assert exc.value.status_code == 422
    assert exc.value.error_code == "VALIDATION_FAILED"
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_a"].id).status == "pending"


# ---------------------------------------------------------------------------
# 换人后的行为联动
# ---------------------------------------------------------------------------


def test_original_task_readonly_after_reassign(db_session, settings, scenario):
    """换人后向原任务提交 → decide_idempotency 对 reassigned 判 INVALID_STATE
    → 409 INVALID_STATE_TRANSITION（不伪造提交）。"""
    reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"],
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        mcp_submit_output(db_session, settings,
                          user_id=scenario["alice"].id,
                          payload=_submit_payload(scenario["task_a"].id))
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"


def test_new_task_drives_collection_chain(db_session, settings, scenario):
    """换人后新任务可走通收齐链路：bob 先提交 → 换 alice → carol 提交新任务
    → maybe_drive_round 收齐翻转。"""
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["bob"].id,
                               payload=_submit_payload(scenario["task_b"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    new_task = reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_a"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"],
    )
    db_session.commit()
    # 新任务提交前不收齐（新任务 pending）
    assert maybe_drive_round(db_session, task_id=scenario["task_b"].id) is None
    result = mcp_submit_output(db_session, settings,
                               user_id=scenario["carol"].id,
                               payload=_submit_payload(new_task.id))
    db_session.commit()
    assert result["status"] == "submitted"
    round_id = maybe_drive_round(db_session, task_id=new_task.id)
    assert round_id == scenario["round"].id
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Round, scenario["round"].id).status == \
        "awaiting_summary"
    assert db_session.get(Matter, scenario["matter"].id).status == \
        "in_progress"

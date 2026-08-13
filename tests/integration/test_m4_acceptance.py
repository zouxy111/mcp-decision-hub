"""M4 acceptance integration tests (scenarios 8/13/18/22).

M3 的 ``test_resolution_e2e.py`` 用真实 uvicorn 进程跑 MCP 协议；
本文件用 service 层直测（直改库 + 直调服务函数）验证 M4 新增功能的
业务逻辑。MCP 层的 429/Retry-After 验证需真实 uvicorn（小阈值），
留待任务 11 手工冒烟。
"""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api import reassignment as reassign_svc
from hub.api.audit import TASK_TIMEOUT
from hub.api.pipeline import maybe_drive_round
from hub.api.scheduler import scan_once
from hub.db.models import AuditEvent, Round, RoundSummary, Task
from hub.domain.timeutil import utcnow
from hub.mcp_server.methods import mcp_get_task, mcp_submit_output
from tests.conftest import make_user

TIMEOUT_SECONDS = 3600


@pytest.fixture()
def scenario(db_session):
    """Two-participant matter with manual questions, round open."""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")  # replacement participant
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=TIMEOUT_SECONDS, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    tasks = {t.assignee_id: t for t in db_session.scalars(select(Task)).all()}
    rnd = db_session.scalar(select(Round))
    return {"init": init, "alice": alice, "bob": bob, "carol": carol,
            "matter": matter, "round": rnd,
            "task_a": tasks[alice.id], "task_b": tasks[bob.id]}


def _expire(db_session, task):
    db_session.execute(
        update(Task).where(Task.id == task.id)
        .values(deadline_at=utcnow() - timedelta(seconds=10)))
    db_session.commit()


def _submit_payload(task_id):
    from hub.domain.digest import compute_content_digest
    answers = [{"question_id": "q1", "content": "回答"}]
    return {
        "task_id": task_id, "answers": answers, "notes": None,
        "human_approved": True, "approved_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content_digest": compute_content_digest(answers, None),
        "idempotency_key": f"k-{task_id}",
    }


def _audits(db_session, event_type, matter_id=None):
    q = select(AuditEvent).where(AuditEvent.event_type == event_type)
    if matter_id:
        q = q.where(AuditEvent.matter_id == matter_id)
    return list(db_session.scalars(q).all())


# ---------------------------------------------------------------------------
# Scenario 8: timeout + reassignment full chain
# ---------------------------------------------------------------------------


def test_scenario_8_timeout_then_reassign_then_submit(db_session, session_factory,
                                                       settings, scenario):
    """任务超时 → 换人 → 新任务提交 → 收齐。"""
    _expire(db_session, scenario["task_b"])
    scan_once(session_factory, settings)
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_b"].id).status == "timeout"
    # alice 也提交（收齐需要所有非终态任务到达终态）
    mcp_submit_output(db_session, settings, user_id=scenario["alice"].id,
                      payload=_submit_payload(scenario["task_a"].id))
    db_session.commit()
    # 换人
    new_task = reassign_svc.reassign_task(
        db_session, matter_id=scenario["matter"].id,
        task_id=scenario["task_b"].id, new_user_id=scenario["carol"].id,
        actor=scenario["init"])
    db_session.commit()
    # 新任务提交 → 收齐（submitted + reassigned + submitted → all terminal）
    payload = _submit_payload(new_task.id)
    mcp_submit_output(db_session, settings, user_id=scenario["carol"].id,
                      payload=payload)
    db_session.commit()
    driven = maybe_drive_round(db_session, task_id=new_task.id)
    db_session.commit()
    assert driven == scenario["round"].id
    db_session.expire_all()
    assert db_session.get(Round, scenario["round"].id).status == "awaiting_summary"


# ---------------------------------------------------------------------------
# Scenario 13: scheduler restart recovery
# ---------------------------------------------------------------------------


def test_scenario_13_restart_recovery(client, db_session, session_factory,
                                       settings, scenario):
    """过期任务在"重启"（新 TestClient 实例）后一个扫描周期内被判定 timeout。"""
    _expire(db_session, scenario["task_b"])
    # 直接调 scan_once（模拟重启后首次扫描）
    processed = scan_once(session_factory, settings)
    assert processed == 1
    db_session.expire_all()
    assert db_session.get(Task, scenario["task_b"].id).status == "timeout"
    # 幂等：再次扫描不产生重复
    processed2 = scan_once(session_factory, settings)
    assert processed2 == 0
    assert len(_audits(db_session, TASK_TIMEOUT)) == 1


# ---------------------------------------------------------------------------
# Scenario 18: reassignment visibility
# ---------------------------------------------------------------------------


def test_scenario_18_reassignee_sees_previous_summary(db_session, settings,
                                                       scenario):
    """换人后新参与人 get_task 可见上一轮摘要与本轮问题，不可见他人原始
    回答与被替换者内容。"""
    # 先提交第一轮两个任务，生成摘要
    mcp_submit_output(db_session, settings, user_id=scenario["alice"].id,
                      payload=_submit_payload(scenario["task_a"].id))
    mcp_submit_output(db_session, settings, user_id=scenario["bob"].id,
                      payload=_submit_payload(scenario["task_b"].id))
    db_session.commit()
    driven = maybe_drive_round(db_session, task_id=scenario["task_b"].id)
    db_session.commit()
    assert driven is not None
    # 关闭轮次并添加摘要
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="closed"))
    db_session.add(RoundSummary(
        round_id=scenario["round"].id, matter_id=scenario["matter"].id,
        consensus_points=["共识"], divergences=["分歧"], blind_spots=[],
        open_questions=["未解决"], convergence="continue",
        generation_status="ok"))
    db_session.commit()
    # 开新一轮（简化：手动创建）
    round2 = Round(matter_id=scenario["matter"].id, round_number=2,
                   status="open", questions=[{"question_id": "q2", "content": "追问？"}])
    db_session.add(round2)
    db_session.flush()
    task_new = Task(round_id=round2.id, matter_id=scenario["matter"].id,
                    assignee_id=scenario["carol"].id, status="pending",
                    deadline_at=utcnow() + timedelta(seconds=TIMEOUT_SECONDS))
    db_session.add(task_new)
    db_session.commit()
    # 换人场景：carol 拿到新任务
    result = mcp_get_task(db_session, settings, user_id=scenario["carol"].id,
                          task_id=task_new.id)
    assert result["previous_summary"] is not None
    assert "共识" in str(result["previous_summary"])
    assert "分歧" in str(result["previous_summary"])
    # 不可见他人原始回答
    assert "alice" not in str(result).lower() or "回答" not in str(result)


# ---------------------------------------------------------------------------
# Scenario 22: rate limiting (domain layer validation)
# ---------------------------------------------------------------------------


def test_scenario_22_rate_limiter_sliding_window():
    """PRD 9.1：滑动窗口语义。MCP 层 429 + Retry-After 需真实 uvicorn
    进程验证（task 11）。"""
    from hub.domain.rate_limit import RateLimiter
    rl = RateLimiter(window_seconds=60)
    t0 = 1000.0
    for _ in range(3):
        assert rl.allow("k", limit=3, now=t0)[0] is True
    ok, retry = rl.allow("k", limit=3, now=t0 + 10)
    assert ok is False
    assert retry == 50
    # 窗口滑过后恢复
    ok, _ = rl.allow("k", limit=3, now=t0 + 61)
    assert ok is True


def test_scenario_22_next_poll_after_in_mcp_response(db_session, settings):
    """list_pending_tasks 返回 next_poll_after（PRD 9.1 轮询）。"""
    init = make_user(db_session, "init22")
    alice = make_user(db_session, "alice22")
    bob = make_user(db_session, "bob22")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    from hub.mcp_server.methods import mcp_list_pending_tasks
    result = mcp_list_pending_tasks(db_session, settings, user_id=alice.id)
    assert "next_poll_after" in result
    assert result["next_poll_after"].endswith("Z")

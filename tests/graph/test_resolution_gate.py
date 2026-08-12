# tests/graph/test_resolution_gate.py
import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_FOLLOWUP_FAILED,
    BLOCKED_REASON_ROUND_LIMIT,
)
from hub.api.resolutions import decide_resolution
from hub.db.models import AuditEvent, Matter, Resolution, Round, RoundSummary, Task
from hub.graph.matter_graph import (
    build_matter_graph,
    drive_matter_tick,
    open_checkpointer,
    resume_matter_gate,
    sqlite_path_from_url,
)
from hub.llm.client import LLMError
from tests.conftest import make_user

SUMMARY_CONVERGED = {
    "consensus_points": ["共识"], "divergences": [], "blind_spots": [],
    "open_questions": [], "convergence": "converged",
}
SUMMARY_PROVISIONAL = {**SUMMARY_CONVERGED,
                       "convergence": "provisionally_ready"}
DRAFT_PAYLOAD = {
    "recommendation": "采用方案 A", "rationale": "依据",
    "risks": ["风险"], "divergences": [], "cited_rounds": [1],
}


def _pending(session_factory, settings, matter_id):
    path = sqlite_path_from_url(settings.database_url)
    saver = open_checkpointer(path)
    try:
        graph = build_matter_graph(
            session_factory=session_factory, settings=settings,
            llm=None, checkpointer=saver,
        )
        return graph.get_state({"configurable": {"thread_id": matter_id}}).next
    finally:
        saver.conn.close()


@pytest.fixture()
def scenario(db_session):
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
    rnd = db_session.scalar(select(Round))
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init}


def _write_summary(db_session, scenario, convergence):
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=[],
            convergence=convergence, generation_status="ok",
        )
    )
    db_session.commit()


def _events(db_session):
    return [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]


def _drive_to_gate(session_factory, settings, scenario, llm):
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)


def test_converged_pauses_at_decision_gate(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ("decision_gate",)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"


def test_tick_while_paused_is_noop(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    _drive_to_gate(session_factory, settings, scenario, llm)
    _drive_to_gate(session_factory, settings, scenario, llm)  # 挂起中：跳过
    assert len(llm.calls) == 1
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ("decision_gate",)


def test_approve_resume_completes_matter(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="approved",
                      expected_version=1)
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "completed"
    events = _events(db_session)
    assert "matter_completed" in events
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ()


def test_resume_propagation_is_idempotent(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="approved",
                      expected_version=1)
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())  # 重放：无重复审计
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "completed"
    assert _events(db_session).count("matter_completed") == 1


def test_decided_but_not_paused_recovers_via_fallback_tick(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """崩溃恢复：决议已落库但线程从未挂起（checkpoint 丢失等价态）——
    resume 兜底 tick 经 branch 的 propagate 路由完成传播（FR-24）。"""
    _write_summary(db_session, scenario, "converged")
    # 不走 tick，直接落库 decided 决议（等价于 resume 全部丢失）
    res = Resolution(
        matter_id=scenario["matter"].id, source_round_id=scenario["round"].id,
        version=2, status="approved", recommendation="R", rationale="J",
        risks=[], divergences=[], cited_rounds=[1], final_text="R",
    )
    db_session.add(res)
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="awaiting_decision")
    )
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "completed"


def test_reject_resume_opens_new_round(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD, {"questions": ["驳回后追问？"]}])
    _drive_to_gate(session_factory, settings, scenario, llm)
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="rejected",
                      expected_version=1, rationale="证据不足")
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide", llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    assert matter.granted_extra_rounds == 0  # 未达上限不授信
    new_round = db_session.scalar(select(Round).where(Round.round_number == 2))
    assert new_round.status == "open"
    assert [q["content"] for q in new_round.questions] == ["驳回后追问？"]
    assert db_session.scalar(
        select(func.count()).select_from(Task).where(Task.round_id == new_round.id)
    ) == 2
    old = db_session.scalar(select(Resolution))
    assert old.status == "rejected"  # 旧草案只读保留
    assert "round_generated" in _events(db_session)


def test_reject_at_round_limit_grants_one_credit(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """场景 19：已达上限时驳回仍允许，隐含授予 +1 额度并直接开新轮。"""
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(max_rounds=1)
    )
    db_session.commit()
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD, {"questions": ["追问？"]}])
    _drive_to_gate(session_factory, settings, scenario, llm)
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="rejected",
                      expected_version=1, rationale="达到上限也要驳回")
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide", llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    assert matter.granted_extra_rounds == 1
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2
    grant_events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "matter_continued"
        and r.detail.get("mode") == "reject_grant"
    ]
    assert len(grant_events) == 1
    assert grant_events[0].detail["rationale"] == "达到上限也要驳回"  # 驳回理由入审计


def test_reject_followup_llm_failure_blocks(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="rejected",
                      expected_version=1, rationale="再议")
    db_session.commit()
    failing = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=failing)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert BLOCKED_REASON_FOLLOWUP_FAILED in matter.blocked_reason
    assert db_session.scalar(select(func.count()).select_from(Round)) == 1


def test_provisional_accept_chains_to_decision_gate(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "provisionally_ready")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ("provisional_gate",)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"
    # 发起人先点"进入拍板"（API 翻状态），再 resume accept
    from hub.api.resolutions import accept_provisional

    accept_provisional(db_session, matter_id=scenario["matter"].id,
                       actor=scenario["init"])
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="accept",
                       llm=make_fake_llm())
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ("decision_gate",)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"


def test_decide_action_chains_through_provisional_gate(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """accept 的状态翻转已发生但 resume 丢失（崩溃）→ 之后 decide 的
    resume 必须链式穿过 provisional_gate 再到 decision_gate。"""
    _write_summary(db_session, scenario, "provisionally_ready")
    _drive_to_gate(session_factory, settings, scenario,
                   make_fake_llm([DRAFT_PAYLOAD]))
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id)
        .values(status="awaiting_decision")  # 等价于 accept_provisional 已落库
    )
    db_session.commit()
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="approved",
                      expected_version=1)
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "completed"


def test_continue_probing_opens_new_round_under_limit(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "provisionally_ready")
    llm = make_fake_llm([DRAFT_PAYLOAD, {"questions": ["继续追问？"]}])
    _drive_to_gate(session_factory, settings, scenario, llm)
    from hub.api.resolutions import continue_probing

    continue_probing(db_session, matter_id=scenario["matter"].id,
                     actor=scenario["init"])
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id,
                       action="continue_probing", llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    assert matter.granted_extra_rounds == 0
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "matter_continued"
        and r.detail.get("mode") == "provisional_continue"
    ]
    assert len(events) == 1
    assert events[0].detail["granted"] is False


def test_continue_probing_at_limit_grants_credit(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(max_rounds=1)
    )
    db_session.commit()
    _write_summary(db_session, scenario, "provisionally_ready")
    llm = make_fake_llm([DRAFT_PAYLOAD, {"questions": ["追问？"]}])
    _drive_to_gate(session_factory, settings, scenario, llm)
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id,
                       action="continue_probing", llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.granted_extra_rounds == 1
    assert matter.status == "collecting"


def test_continue_probing_resume_is_idempotent(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "provisionally_ready")
    llm = make_fake_llm([DRAFT_PAYLOAD, {"questions": ["追问？"]}])
    _drive_to_gate(session_factory, settings, scenario, llm)
    for _ in range(2):
        resume_matter_gate(session_factory, settings,
                           matter_id=scenario["matter"].id,
                           action="continue_probing", llm=llm)
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2
    assert len(llm.calls) == 2  # 追问 LLM 只调一次（第二次无新轮可建）


def test_manual_draft_from_blocked_flows_through_gates(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """任务 9 的手工草案入口与闸门的链路：轮次上限 blocked 直接生成草案
    后，tick 把图停在 provisional_gate（源轮摘要为 continue），decide 的
    resume 链式穿过两个闸门完成传播。"""
    _write_summary(db_session, scenario, "continue")
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_ROUND_LIMIT)
    )
    db_session.commit()
    from hub.api.resolutions import draft_resolution_from_blocked

    draft_resolution_from_blocked(
        db_session, matter_id=scenario["matter"].id,
        actor=scenario["init"], llm=make_fake_llm([DRAFT_PAYLOAD]))
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=make_fake_llm())
    assert _pending(session_factory, settings,
                    scenario["matter"].id) == ("provisional_gate",)
    decide_resolution(db_session, matter_id=scenario["matter"].id,
                      actor=scenario["init"], decision="approved",
                      expected_version=1)
    db_session.commit()
    resume_matter_gate(session_factory, settings,
                       matter_id=scenario["matter"].id, action="decide",
                       llm=make_fake_llm())
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "completed"

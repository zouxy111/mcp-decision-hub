import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import BLOCKED_REASON_DRAFT_FAILED, run_round_pipeline
from hub.db.models import AuditEvent, Matter, Resolution, Round, RoundSummary
from hub.llm.client import LLMError
from tests.conftest import make_user

DRAFT_PAYLOAD = {
    "recommendation": "采用方案 A，分两期实施",
    "rationale": "两轮讨论后关键分歧已收敛",
    "risks": ["进度风险"],
    "divergences": ["成本口径仍未完全对齐"],
    "cited_rounds": [1],
}


@pytest.fixture()
def scenario(db_session):
    """Round 1 closed with an ok ready-state summary; matter in_progress."""
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


def test_converged_generates_draft_and_awaits_decision(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema_name"] == "resolution_draft"
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "awaiting_decision"
    res = db_session.scalar(select(Resolution))
    assert res is not None
    assert res.status == "pending_review"
    assert res.version == 1
    assert res.matter_id == matter.id
    assert res.source_round_id == scenario["round"].id
    assert res.recommendation == "采用方案 A，分两期实施"
    assert res.rationale == "两轮讨论后关键分歧已收敛"
    assert res.risks == ["进度风险"]
    assert res.divergences == ["成本口径仍未完全对齐"]
    assert res.cited_rounds == [1]
    assert res.final_text is None
    assert res.decided_at is None
    events = _events(db_session)
    assert "resolution_drafted" in events
    assert "matter_awaiting_decision" in events


def test_provisional_generates_draft_and_pauses_in_progress(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """PRD 7.1/7.6: provisionally_ready 生成草案并暂停，matter 停留
    in_progress，等发起人选择继续追问或进入拍板。"""
    _write_summary(db_session, scenario, "provisionally_ready")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 1
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"
    res = db_session.scalar(select(Resolution))
    assert res is not None
    assert res.status == "pending_review"
    events = _events(db_session)
    assert "resolution_drafted" in events
    assert "matter_awaiting_decision" not in events


def test_draft_prompt_carries_all_round_summaries(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """草案输入是全部轮次的 ok 摘要（设计 §5），按轮次升序。"""
    _write_summary(db_session, scenario, "continue")
    rnd2 = Round(matter_id=scenario["matter"].id, round_number=2,
                 status="closed",
                 questions=[{"question_id": "q1", "content": "追问？"}])
    db_session.add(rnd2)
    db_session.flush()
    db_session.add(
        RoundSummary(
            round_id=rnd2.id, matter_id=scenario["matter"].id,
            consensus_points=["第二轮共识"], divergences=[],
            blind_spots=[], open_questions=[],
            convergence="converged", generation_status="ok",
        )
    )
    db_session.commit()
    llm = make_fake_llm([{**DRAFT_PAYLOAD, "cited_rounds": [1, 2]}])
    run_round_pipeline(session_factory, settings, round_id=rnd2.id, llm=llm)
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["user_prompt"]
    assert "共识" in prompt
    assert "第二轮共识" in prompt
    assert prompt.index("共识") < prompt.index("第二轮共识")
    db_session.expire_all()
    res = db_session.scalar(select(Resolution))
    assert res.source_round_id == rnd2.id
    assert res.cited_rounds == [1, 2]


def test_draft_failure_blocks_with_error_detail(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert BLOCKED_REASON_DRAFT_FAILED in matter.blocked_reason
    assert "LLM_TIMEOUT" in matter.blocked_reason
    assert "3" in matter.blocked_reason
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 0
    events = _events(db_session)
    assert "llm_failed" in events


def test_redrive_does_not_duplicate_draft(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1
    assert len(llm.calls) == 1  # 第二次未再调用 LLM


def test_next_draft_version_increments_after_rejection(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """FR-21b：每个草案 version 单调递增（驳回后新草案 = max+1）。
    构造：第 1 轮已有一份 rejected v1 草案，第 2 轮 converged 触发新草案。"""
    _write_summary(db_session, scenario, "continue")
    db_session.add(
        Resolution(
            matter_id=scenario["matter"].id,
            source_round_id=scenario["round"].id, version=1,
            status="rejected", recommendation="旧草案", rationale="旧依据",
            risks=[], divergences=[], cited_rounds=[1],
        )
    )
    rnd2 = Round(matter_id=scenario["matter"].id, round_number=2,
                 status="closed",
                 questions=[{"question_id": "q1", "content": "追问？"}])
    db_session.add(rnd2)
    db_session.flush()
    db_session.add(
        RoundSummary(
            round_id=rnd2.id, matter_id=scenario["matter"].id,
            consensus_points=["第二轮共识"], divergences=[],
            blind_spots=[], open_questions=[],
            convergence="converged", generation_status="ok",
        )
    )
    db_session.commit()
    llm = make_fake_llm([DRAFT_PAYLOAD])
    run_round_pipeline(session_factory, settings, round_id=rnd2.id, llm=llm)
    db_session.expire_all()
    versions = db_session.scalars(
        select(Resolution.version).order_by(Resolution.version)
    ).all()
    assert versions == [1, 2]

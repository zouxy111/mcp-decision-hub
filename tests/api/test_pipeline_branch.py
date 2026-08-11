import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_LLM_BLOCKED,
    BLOCKED_REASON_ROUND_LIMIT,
    run_round_pipeline,
)
from hub.db.models import AuditEvent, Matter, Round, RoundSummary, Task
from hub.llm.client import LLMError
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
    """Round 1 closed with an ok summary; matter in_progress."""
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
    db_session.execute(
        update(Round).where(Round.id == rnd.id).values(status="closed")
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.commit()
    return {"matter": matter, "round": rnd}


def _write_summary(db_session, scenario, convergence):
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=["未决"],
            convergence=convergence, generation_status="ok",
        )
    )
    db_session.commit()


def test_continue_generates_next_round(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    llm = make_fake_llm([{"questions": ["追问一？", "追问二？"]}])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema_name"] == "questions"
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    new_round = db_session.scalar(
        select(Round).where(Round.round_number == 2)
    )
    assert new_round.status == "open"
    assert [q["question_id"] for q in new_round.questions] == ["q1", "q2"]
    assert [q["content"] for q in new_round.questions] == ["追问一？", "追问二？"]
    tasks = db_session.scalars(
        select(Task).where(Task.round_id == new_round.id)
    ).all()
    assert len(tasks) == 2
    assert all(t.status == "pending" for t in tasks)
    assert all(t.deadline_at is not None for t in tasks)
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "round_generated" in events


def test_continue_at_limit_blocks(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(max_rounds=1)  # 第 1 轮已达上限
    )
    db_session.commit()
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0  # 达上限不出题
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_ROUND_LIMIT
    assert db_session.scalar(select(func.count()).select_from(Round)) == 1


def test_continue_with_granted_credit_advances(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(max_rounds=1, granted_extra_rounds=1)
    )
    db_session.commit()
    llm = make_fake_llm([{"questions": ["追问？"]}])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "collecting"
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2


@pytest.mark.parametrize("convergence", ["provisionally_ready", "converged"])
def test_ready_states_go_awaiting_decision(
    db_session, session_factory, settings, scenario, make_fake_llm, convergence
):
    _write_summary(db_session, scenario, convergence)
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"


def test_llm_blocked_convergence_blocks_matter(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "blocked")
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_LLM_BLOCKED


def test_followup_failure_blocks_with_error_detail(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    llm = make_fake_llm([LLMError("LLM_AUTH_FAILED", "认证失败", retry_count=3)])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert "定向追问出题失败" in matter.blocked_reason
    assert "LLM_AUTH_FAILED" in matter.blocked_reason
    assert "3" in matter.blocked_reason
    assert db_session.scalar(select(func.count()).select_from(Round)) == 1
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "llm_failed" in events


def test_redrive_does_not_duplicate_round(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    llm = make_fake_llm([{"questions": ["追问？"]}])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    # 模拟重复入队再次驱动同一轮
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2
    assert len(llm.calls) == 1  # 第二次未再调用 LLM


def test_not_latest_round_is_ignored(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _write_summary(db_session, scenario, "continue")
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="collecting")  # 新轮已开，事项回到 collecting
    )
    db_session.add(
        Round(matter_id=scenario["matter"].id, round_number=2, status="open",
              questions=[{"question_id": "q1", "content": "已有新轮"}])
    )
    db_session.commit()
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2

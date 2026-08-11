import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_NO_OUTPUT,
    BLOCKED_REASON_SUMMARY_FAILED,
    run_round_pipeline,
)
from hub.db.models import AuditEvent, Matter, Output, Round, RoundSummary, Task
from hub.domain.timeutil import utcnow
from hub.llm.client import LLMError
from tests.conftest import make_user

SUMMARY_PAYLOAD = {
    "consensus_points": ["都认可方向 X"],
    "divergences": ["成本口径不一致"],
    "blind_spots": [],
    "open_questions": ["运维成本如何估算？"],
    "convergence": "continue",
}


@pytest.fixture()
def scenario(db_session):
    """Matter with round 1 awaiting_summary, both tasks submitted."""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?", "Q2?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    rnd = db_session.scalar(select(Round))
    for t in db_session.scalars(select(Task)).all():
        db_session.execute(
            update(Task).where(Task.id == t.id)
            .values(status="submitted", submitted_at=utcnow())
        )
        db_session.add(
            Output(
                task_id=t.id,
                answers=[{"question_id": "q1", "content": f"{t.assignee_id} 的回答"}],
                notes=None, approved_at=utcnow(), content_digest="x" * 64,
            )
        )
    db_session.execute(
        update(Round).where(Round.id == rnd.id).values(status="awaiting_summary")
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.commit()
    return {"matter": matter, "round": rnd}


def _summary_count(db_session, round_id) -> int:
    return db_session.scalar(
        select(func.count()).select_from(RoundSummary)
        .where(RoundSummary.round_id == round_id)
    )


def test_success_writes_summary_and_closes_round(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    # 脚本两项：摘要 + 定向追问（任务 10 分支阶段消费第二项）
    llm = make_fake_llm([SUMMARY_PAYLOAD, {"questions": ["追问一？"]}])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    summary = db_session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == scenario["round"].id)
    )
    assert summary.generation_status == "ok"
    assert summary.consensus_points == ["都认可方向 X"]
    assert summary.convergence == "continue"
    assert summary.error_code is None
    assert db_session.get(Round, scenario["round"].id).status == "closed"
    assert len(llm.calls) == 2
    types = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "round_summarized" in types
    assert "convergence_decided" in types


def test_existing_ok_summary_skips_llm(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=["已有"], divergences=[], blind_spots=[],
            open_questions=[], convergence="converged", generation_status="ok",
        )
    )
    db_session.commit()
    llm = make_fake_llm()  # 无脚本：任何调用都会抛错
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0
    assert _summary_count(db_session, scenario["round"].id) == 1


def test_llm_failure_marks_failed_and_blocks(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    llm = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    summary = db_session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == scenario["round"].id)
    )
    assert summary.generation_status == "failed"
    assert summary.error_code == "LLM_TIMEOUT"
    assert summary.retry_count == 3
    assert summary.convergence is None
    assert db_session.get(Round, scenario["round"].id).status == "failed"
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_SUMMARY_FAILED
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "llm_failed" in events
    assert "matter_blocked" in events


def test_failed_summary_row_is_terminal(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=[], divergences=[], blind_spots=[], open_questions=[],
            convergence=None, generation_status="failed",
            error_code="LLM_TIMEOUT", retry_count=3,
        )
    )
    db_session.commit()
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0
    assert _summary_count(db_session, scenario["round"].id) == 1


def test_zero_submitted_never_calls_llm(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    # 全部超时：直接改库（M2 无超时调度器）
    from sqlalchemy import delete

    for t in db_session.scalars(select(Task)).all():
        db_session.execute(
            update(Task).where(Task.id == t.id).values(status="timeout")
        )
        db_session.execute(delete(Output).where(Output.task_id == t.id))
    db_session.commit()
    llm = make_fake_llm([SUMMARY_PAYLOAD])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert len(llm.calls) == 0
    db_session.expire_all()
    assert db_session.get(Round, scenario["round"].id).status == "closed"
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_NO_OUTPUT
    assert _summary_count(db_session, scenario["round"].id) == 0
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "matter_blocked" in events

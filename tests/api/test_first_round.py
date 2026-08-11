# tests/api/test_first_round.py
import pytest
from sqlalchemy import func, select

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_FIRST_ROUND_FAILED,
    run_round_pipeline,
)
from hub.db.models import AuditEvent, Matter, Round, Task
from hub.llm.client import LLMError
from tests.conftest import make_user


@pytest.fixture()
def llm_matter(db_session):
    """draft matter without manual questions (LLM first-round path)."""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=[],
    )
    db_session.commit()
    return {"init": init, "matter": matter}


def _start(db_session, scenario):
    matter_svc.start_matter(db_session, matter_id=scenario["matter"].id,
                            actor=scenario["init"])
    db_session.commit()
    return db_session.scalar(select(Round))


def test_start_llm_path_stays_in_progress_with_generating_round(
    db_session, llm_matter
):
    rnd = _start(db_session, llm_matter)
    db_session.expire_all()
    matter = db_session.get(Matter, llm_matter["matter"].id)
    assert matter.status == "in_progress"  # 不是 collecting（尚无 open 轮次）
    assert rnd.status == "generating"
    assert rnd.questions == []
    assert db_session.scalar(select(func.count()).select_from(Task)) == 0


def test_llm_first_round_success(
    db_session, session_factory, settings, llm_matter, make_fake_llm
):
    rnd = _start(db_session, llm_matter)
    llm = make_fake_llm([{"questions": ["问题一？", "问题二？", "问题三？"]}])
    run_round_pipeline(session_factory, settings, round_id=rnd.id, llm=llm)
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema_name"] == "questions"
    db_session.expire_all()
    rnd = db_session.get(Round, rnd.id)
    assert rnd.status == "open"
    # question_id 服务端分配，LLM 只提供文本（PRD 9.1）
    assert [q["question_id"] for q in rnd.questions] == ["q1", "q2", "q3"]
    assert [q["content"] for q in rnd.questions] == ["问题一？", "问题二？", "问题三？"]
    tasks = db_session.scalars(select(Task).where(Task.round_id == rnd.id)).all()
    assert len(tasks) == 2
    assert all(t.status == "pending" and t.deadline_at is not None for t in tasks)
    assert db_session.get(Matter, llm_matter["matter"].id).status == "collecting"
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "round_generated" in events


def test_llm_first_round_failure_blocks_with_error(
    db_session, session_factory, settings, llm_matter, make_fake_llm
):
    rnd = _start(db_session, llm_matter)
    llm = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    run_round_pipeline(session_factory, settings, round_id=rnd.id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Round, rnd.id).status == "failed"
    matter = db_session.get(Matter, llm_matter["matter"].id)
    assert matter.status == "blocked"
    assert BLOCKED_REASON_FIRST_ROUND_FAILED in matter.blocked_reason
    assert "LLM_TIMEOUT" in matter.blocked_reason
    assert "3" in matter.blocked_reason
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "llm_failed" in events
    assert "matter_blocked" in events


def test_llm_first_round_redrive_is_idempotent(
    db_session, session_factory, settings, llm_matter, make_fake_llm
):
    rnd = _start(db_session, llm_matter)
    llm = make_fake_llm([{"questions": ["问题？"]}])
    run_round_pipeline(session_factory, settings, round_id=rnd.id, llm=llm)
    run_round_pipeline(session_factory, settings, round_id=rnd.id, llm=llm)
    assert len(llm.calls) == 1  # 第二次驱动未再调 LLM
    assert db_session.scalar(select(func.count()).select_from(Task)) == 2


def test_manual_questions_never_call_llm(
    db_session, session_factory, settings, make_fake_llm
):
    init = make_user(db_session, "init_m")
    alice = make_user(db_session, "alice_m")
    bob = make_user(db_session, "bob_m")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["手动问题？"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    assert db_session.get(Matter, matter.id).status == "collecting"
    rnd = db_session.scalar(select(Round))
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings, round_id=rnd.id, llm=llm)
    assert len(llm.calls) == 0
    assert db_session.get(Round, rnd.id).status == "open"

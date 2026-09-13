from datetime import timedelta

import pytest
from sqlalchemy import select

from hub.db.models import (
    AgentToken,
    AuditEvent,
    IdempotencyRecord,
    Matter,
    MatterParticipant,
    Output,
    Round,
    Task,
)
from hub.domain.timeutil import utcnow
from tests.conftest import make_user


def test_full_chain_roundtrip(db_session):
    initiator = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    matter = Matter(
        initiator_id=initiator.id, title="t", goal="g", background="b",
        status="draft", timeout_seconds=3600, max_rounds=10,
        initiator_participates=False, draft_questions=["Q1?"],
    )
    db_session.add(matter)
    db_session.flush()
    db_session.add(MatterParticipant(matter_id=matter.id, user_id=alice.id))
    rnd = Round(matter_id=matter.id, round_number=1, status="open",
                questions=[{"question_id": "q1", "content": "Q1?"}])
    db_session.add(rnd)
    db_session.flush()
    task = Task(round_id=rnd.id, matter_id=matter.id, assignee_id=alice.id,
                status="pending", deadline_at=utcnow() + timedelta(hours=1))
    db_session.add(task)
    db_session.flush()
    out = Output(task_id=task.id, answers=[{"question_id": "q1", "content": "A"}],
                 notes=None, approved_at=utcnow(), content_digest="x" * 64)
    rec = IdempotencyRecord(scope_type="task", scope_id=task.id, task_id=task.id,
                            idempotency_key="k1",
                            request_fingerprint="f", response_json="{}")
    tok = AgentToken(user_id=alice.id, name="a1", token_hash="h" * 64)
    evt = AuditEvent(actor_user_id=alice.id, event_type="task_submitted",
                     matter_id=matter.id, detail={"task_id": task.id})
    db_session.add_all([out, rec, tok, evt])
    db_session.commit()

    loaded = db_session.scalar(select(Task).where(Task.assignee_id == alice.id))
    assert loaded.status == "pending"
    assert loaded.matter_id == matter.id
    assert db_session.scalar(select(Output).where(Output.task_id == task.id)) is not None
    assert db_session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.task_id == task.id,
            IdempotencyRecord.idempotency_key == "k1",
        )
    ) is not None


def test_task_id_has_prefix(db_session):
    from hub.db.models import new_id

    assert new_id("tsk").startswith("tsk_")
    assert new_id("mat") != new_id("mat")


def test_round_summary_roundtrip_and_unique(db_session):
    from sqlalchemy.exc import IntegrityError

    from hub.db.models import RoundSummary

    initiator = make_user(db_session, "init2")
    matter = Matter(
        initiator_id=initiator.id, title="t", goal="g", background="b",
        status="collecting", timeout_seconds=3600, max_rounds=10,
        initiator_participates=False, draft_questions=["Q1?"],
    )
    db_session.add(matter)
    db_session.flush()
    rnd = Round(matter_id=matter.id, round_number=1, status="open",
                questions=[{"question_id": "q1", "content": "Q1?"}])
    db_session.add(rnd)
    db_session.flush()
    summary = RoundSummary(
        round_id=rnd.id, matter_id=matter.id,
        consensus_points=["共识一"], divergences=["分歧一"],
        blind_spots=[], open_questions=["问题一"],
        convergence="continue", generation_status="ok",
    )
    db_session.add(summary)
    db_session.commit()
    assert db_session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == rnd.id)
    ).convergence == "continue"

    duplicate = RoundSummary(
        round_id=rnd.id, matter_id=matter.id,
        consensus_points=[], divergences=[], blind_spots=[], open_questions=[],
        convergence=None, generation_status="failed",
        error_code="LLM_TIMEOUT", retry_count=3,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_matter_new_columns_defaults(db_session):
    initiator = make_user(db_session, "init3")
    matter = Matter(
        initiator_id=initiator.id, title="t", goal="g", background="b",
        status="draft", timeout_seconds=3600, max_rounds=10,
        initiator_participates=False, draft_questions=["Q1?"],
    )
    db_session.add(matter)
    db_session.commit()
    assert matter.granted_extra_rounds == 0
    assert matter.blocked_reason is None

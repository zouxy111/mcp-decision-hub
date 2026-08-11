import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_NO_OUTPUT,
    BLOCKED_REASON_ROUND_LIMIT,
    maybe_drive_round,
)
from hub.db.models import Matter, Round, Task
from tests.conftest import make_user


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
    task_a = db_session.scalar(select(Task).where(Task.assignee_id == alice.id))
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    return {"matter": matter, "task_a": task_a, "task_b": task_b}


def test_not_collected_returns_none(db_session, scenario):
    db_session.execute(
        Task.__table__.update()
        .where(Task.id == scenario["task_a"].id)
        .values(status="submitted")
    )
    db_session.commit()
    assert maybe_drive_round(db_session, task_id=scenario["task_a"].id) is None
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "collecting"
    assert db_session.get(Round, scenario["task_a"].round_id).status == "open"


def test_collected_flips_states_and_returns_round_id(db_session, scenario):
    for t in (scenario["task_a"], scenario["task_b"]):
        db_session.execute(
            Task.__table__.update().where(Task.id == t.id).values(status="submitted")
        )
    db_session.commit()
    round_id = maybe_drive_round(db_session, task_id=scenario["task_a"].id)
    assert round_id == scenario["task_a"].round_id
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"
    assert db_session.get(Round, round_id).status == "awaiting_summary"


def test_second_call_is_idempotent(db_session, scenario):
    for t in (scenario["task_a"], scenario["task_b"]):
        db_session.execute(
            Task.__table__.update().where(Task.id == t.id).values(status="submitted")
        )
    db_session.commit()
    assert maybe_drive_round(db_session, task_id=scenario["task_a"].id) is not None
    assert maybe_drive_round(db_session, task_id=scenario["task_b"].id) is None


def test_timeout_and_reassigned_count_towards_collection(db_session, scenario):
    db_session.execute(
        Task.__table__.update()
        .where(Task.id == scenario["task_a"].id).values(status="timeout")
    )
    db_session.execute(
        Task.__table__.update()
        .where(Task.id == scenario["task_b"].id).values(status="reassigned")
    )
    db_session.commit()
    assert maybe_drive_round(db_session, task_id=scenario["task_a"].id) is not None


def test_unknown_task_returns_none(db_session):
    assert maybe_drive_round(db_session, task_id="tsk_nonexistent") is None


def test_blocked_reason_constants():
    assert BLOCKED_REASON_NO_OUTPUT == "本轮无有效输出"
    assert BLOCKED_REASON_ROUND_LIMIT == "达到轮次上限"

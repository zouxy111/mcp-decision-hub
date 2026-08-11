import uuid

import pytest
from sqlalchemy import func, select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import AuditEvent, Output, Task
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from hub.mcp_server.methods import mcp_submit_output
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
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
    task_a = db_session.scalar(select(Task).where(Task.assignee_id == alice.id))
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    return {"init": init, "alice": alice, "bob": bob, "matter": matter,
            "task_a": task_a, "task_b": task_b}


def _payload(task_id, **overrides):
    answers = [{"question_id": "q1", "content": "回答一"},
               {"question_id": "q2", "content": "回答二"}]
    payload = {
        "task_id": task_id,
        "answers": answers,
        "notes": "备注",
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, "备注"),
        "idempotency_key": str(uuid.uuid4()),
    }
    payload.update(overrides)
    if "answers" in overrides or "notes" in overrides:
        payload["content_digest"] = compute_content_digest(
            payload["answers"], payload.get("notes")
        )
    return payload


def _submit(db_session, settings, user_id, payload):
    return mcp_submit_output(db_session, settings, user_id=user_id, payload=payload)


def test_happy_path_submits_and_transitions(db_session, settings, scenario):
    result = _submit(db_session, settings, scenario["alice"].id,
                     _payload(scenario["task_a"].id))
    db_session.commit()
    assert result["status"] == "submitted"
    assert result["submitted_at"].endswith("Z")
    db_session.expire_all()
    task = db_session.get(Task, scenario["task_a"].id)
    assert task.status == "submitted"
    out = db_session.scalar(select(Output).where(Output.task_id == task.id))
    assert out.content_digest == compute_content_digest(
        _payload(task.id)["answers"], "备注")
    assert out.approved_at is not None
    types = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "task_submitted" in types


def test_missing_human_approved_422(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    payload["human_approved"] = False
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.status_code == 422
    assert exc.value.error_code == "HUMAN_APPROVAL_REQUIRED"
    assert db_session.get(Task, scenario["task_a"].id).status == "pending"


def test_missing_approved_at_422(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    del payload["approved_at"]
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "HUMAN_APPROVAL_REQUIRED"


def test_future_approved_at_422(db_session, settings, scenario):
    from datetime import timedelta

    payload = _payload(scenario["task_a"].id,
                       approved_at=iso_z(utcnow() + timedelta(minutes=1)))
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "HUMAN_APPROVAL_REQUIRED"


def test_digest_mismatch_422(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id, content_digest="0" * 64)
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "HUMAN_APPROVAL_REQUIRED"
    assert "摘要" in exc.value.message


def test_unknown_question_422(db_session, settings, scenario):
    payload = _payload(
        scenario["task_a"].id,
        answers=[{"question_id": "qX", "content": "x"}],
    )
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "QUESTION_INVALID"


def test_duplicate_question_422(db_session, settings, scenario):
    payload = _payload(
        scenario["task_a"].id,
        answers=[{"question_id": "q1", "content": "a"},
                 {"question_id": "q1", "content": "b"}],
    )
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "QUESTION_INVALID"


def test_empty_answers_422(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id, answers=[])
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "QUESTION_INVALID"


def test_partial_answers_ok(db_session, settings, scenario):
    # PRD 6.3.1: partial answers allowed, minimum 1 item
    payload = _payload(scenario["task_a"].id,
                       answers=[{"question_id": "q2", "content": "只答第二题"}])
    result = _submit(db_session, settings, scenario["alice"].id, payload)
    assert result["status"] == "submitted"


def test_oversize_content_422_with_details(db_session, settings, scenario):
    payload = _payload(
        scenario["task_a"].id,
        answers=[{"question_id": "q1", "content": "a" * (16 * 1024 + 1)}],
    )
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "CONTENT_LIMIT_EXCEEDED"
    assert exc.value.details["limit"] == "answers[].content"
    assert exc.value.details["actual"] == 16 * 1024 + 1


def test_other_users_task_403(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id,
                _payload(scenario["task_b"].id))
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FORBIDDEN_SCOPE"


def test_missing_idempotency_key_422(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    del payload["idempotency_key"]
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, payload)
    assert exc.value.error_code == "VALIDATION_FAILED"


def test_replay_same_key_same_body(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    first = _submit(db_session, settings, scenario["alice"].id, payload)
    db_session.commit()
    second = _submit(db_session, settings, scenario["alice"].id, dict(payload))
    assert second == first  # exact first response, submitted_at not refreshed
    assert db_session.scalar(
        select(func.count()).select_from(Output).where(
            Output.task_id == scenario["task_a"].id)
    ) == 1


def test_same_key_different_body_409(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    _submit(db_session, settings, scenario["alice"].id, payload)
    db_session.commit()
    other = _payload(scenario["task_a"].id,
                     answers=[{"question_id": "q1", "content": "改了"}],
                     idempotency_key=payload["idempotency_key"])
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, other)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "IDEMPOTENCY_CONFLICT"


def test_new_key_same_content_replays_with_audit(db_session, settings, scenario):
    payload = _payload(scenario["task_a"].id)
    first = _submit(db_session, settings, scenario["alice"].id, payload)
    db_session.commit()
    replay = _payload(scenario["task_a"].id)  # new key, identical content
    second = _submit(db_session, settings, scenario["alice"].id, replay)
    assert second["submitted_at"] == first["submitted_at"]
    rows = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "output_replayed")
    ).all()
    assert len(rows) == 1


def test_new_key_different_content_409(db_session, settings, scenario):
    _submit(db_session, settings, scenario["alice"].id,
            _payload(scenario["task_a"].id))
    db_session.commit()
    other = _payload(scenario["task_a"].id,
                     answers=[{"question_id": "q1", "content": "不同内容"}])
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id, other)
    assert exc.value.error_code == "TASK_ALREADY_SUBMITTED"


def test_submit_to_timeout_task_409(db_session, settings, scenario):
    task = db_session.get(Task, scenario["task_a"].id)
    task.status = "timeout"  # direct DB manipulation (no scheduler in M1)
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        _submit(db_session, settings, scenario["alice"].id,
                _payload(scenario["task_a"].id))
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"
    assert db_session.scalar(
        select(func.count()).select_from(Output).where(
            Output.task_id == scenario["task_a"].id)
    ) == 0

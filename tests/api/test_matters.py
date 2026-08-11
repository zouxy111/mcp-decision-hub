import pytest
from sqlalchemy import func, select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import AuditEvent, MatterParticipant, Round, Task
from tests.conftest import make_user


def _three_users(db_session):
    init = make_user(db_session, "init")
    a = make_user(db_session, "alice")
    b = make_user(db_session, "bob")
    return init, a, b


def _create(db_session, initiator, participant_ids, **overrides):
    kwargs = dict(
        initiator=initiator, title="选题", goal="目标", background="背景",
        participant_ids=participant_ids, initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?", "Q2?"],
    )
    kwargs.update(overrides)
    return matter_svc.create_matter(db_session, **kwargs)


def test_create_draft_persists_participants(db_session):
    init, a, b = _three_users(db_session)
    matter = _create(db_session, init, [a.id, b.id])
    assert matter.status == "draft"
    roster = db_session.scalars(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter.id)
    ).all()
    assert sorted(roster) == sorted([a.id, b.id])
    types = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "matter_created" in types


def test_create_rejects_bad_participant_count(db_session):
    init, a, _ = _three_users(db_session)
    with pytest.raises(ApiError) as exc:
        _create(db_session, init, [a.id])
    assert exc.value.status_code == 422
    assert exc.value.error_code == "PARTICIPANT_COUNT_INVALID"


def test_create_rejects_unknown_participant(db_session):
    init, a, _ = _three_users(db_session)
    with pytest.raises(ApiError) as exc:
        _create(db_session, init, [a.id, 99999])
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_create_rejects_empty_questions(db_session):
    init, a, b = _three_users(db_session)
    with pytest.raises(ApiError) as exc:
        _create(db_session, init, [a.id, b.id], draft_questions=["  ", ""])
    assert exc.value.error_code == "QUESTION_INVALID"


def test_create_with_initiator_self_answer(db_session):
    init, a, b = _three_users(db_session)
    matter = _create(db_session, init, [init.id, a.id, b.id],
                     initiator_participates=True)
    roster = db_session.scalars(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter.id)
    ).all()
    assert init.id in roster


def test_start_generates_round_and_tasks(db_session):
    init, a, b = _three_users(db_session)
    matter = _create(db_session, init, [a.id, b.id])
    started = matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    assert started.status == "collecting"
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    assert rnd.status == "open"
    assert rnd.round_number == 1
    qids = [q["question_id"] for q in rnd.questions]
    assert qids == ["q1", "q2"]
    assert len(set(qids)) == len(qids)  # unique within round
    tasks = db_session.scalars(select(Task).where(Task.round_id == rnd.id)).all()
    assert len(tasks) == 2
    assert {t.assignee_id for t in tasks} == {a.id, b.id}
    assert all(t.status == "pending" for t in tasks)
    assert all(t.deadline_at is not None for t in tasks)
    types = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "matter_started" in types


def test_start_by_non_initiator_403_and_audited(db_session):
    init, a, b = _three_users(db_session)
    matter = _create(db_session, init, [a.id, b.id])
    with pytest.raises(ApiError) as exc:
        matter_svc.start_matter(db_session, matter_id=matter.id, actor=a)
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FORBIDDEN_SCOPE"
    rows = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "forbidden_denied")
    ).all()
    assert len(rows) == 1


def test_double_start_409_invalid_state(db_session):
    init, a, b = _three_users(db_session)
    matter = _create(db_session, init, [a.id, b.id])
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    with pytest.raises(ApiError) as exc:
        matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "INVALID_STATE_TRANSITION"
    rows = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "invalid_state_transition")
    ).all()
    assert len(rows) == 1
    # only one round created
    assert db_session.scalar(
        select(func.count()).select_from(Round).where(Round.matter_id == matter.id)
    ) == 1


def test_start_missing_matter_404(db_session):
    init, _, _ = _three_users(db_session)
    with pytest.raises(ApiError) as exc:
        matter_svc.start_matter(db_session, matter_id="mat_nope", actor=init)
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_list_and_get_matter_visibility(db_session):
    init, a, b = _three_users(db_session)
    outsider = make_user(db_session, "outsider")
    matter = _create(db_session, init, [a.id, b.id])
    assert matter_svc.get_matter_for_user(
        db_session, matter_id=matter.id, user=init) is not None
    assert matter_svc.get_matter_for_user(
        db_session, matter_id=matter.id, user=a) is not None
    assert matter_svc.get_matter_for_user(
        db_session, matter_id=matter.id, user=outsider) is None
    assert [m.id for m in matter_svc.list_matters_for_user(db_session, user=a)] == [
        matter.id]
    assert matter_svc.list_matters_for_user(db_session, user=outsider) == []
    assert matter_svc.is_participant(db_session, matter_id=matter.id, user_id=a.id)
    assert not matter_svc.is_participant(
        db_session, matter_id=matter.id, user_id=init.id)

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import AuditEvent
from hub.mcp_server.methods import mcp_get_matter_status
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    outsider = make_user(db_session, "outsider")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    return {"init": init, "alice": alice, "bob": bob, "outsider": outsider,
            "matter": matter}


def test_non_participating_initiator_gets_200(db_session, settings, scenario):
    # PRD 3.0 / acceptance 25: initiator without a task must get 200, not 403
    result = mcp_get_matter_status(
        db_session, settings, user_id=scenario["init"].id,
        matter_id=scenario["matter"].id,
    )
    assert result["status"] == "collecting"
    assert result["rounds_total"] == 1
    assert len(result["recent_rounds"]) == 1
    rv = result["recent_rounds"][0]
    assert rv["round_number"] == 1
    assert rv["tasks_total"] == 2
    assert rv["tasks_submitted"] == 0
    assert rv["summaries"] == []  # M1: empty, never fabricated
    assert result["resolution"] is None
    # initiator view: per-participant progress
    names = {p["username"] for p in result["participant_progress"]}
    assert names == {"alice", "bob"}
    assert all(p["task_status"] == "pending"
               for p in result["participant_progress"])


def test_participant_gets_200_without_progress_detail(db_session, settings,
                                                      scenario):
    result = mcp_get_matter_status(
        db_session, settings, user_id=scenario["alice"].id,
        matter_id=scenario["matter"].id,
    )
    assert result["status"] == "collecting"
    assert "participant_progress" not in result  # initiator-only field


def test_outsider_404_and_audited(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        mcp_get_matter_status(
            db_session, settings, user_id=scenario["outsider"].id,
            matter_id=scenario["matter"].id,
        )
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"
    rows = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "forbidden_denied")
    ).all()
    assert len(rows) == 1


def test_missing_matter_404(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        mcp_get_matter_status(
            db_session, settings, user_id=scenario["init"].id,
            matter_id="mat_nope",
        )
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_rounds_before_cursor(db_session, settings, scenario):
    result = mcp_get_matter_status(
        db_session, settings, user_id=scenario["init"].id,
        matter_id=scenario["matter"].id, rounds_before=1,
    )
    assert result["recent_rounds"] == []
    assert result["rounds_total"] == 1

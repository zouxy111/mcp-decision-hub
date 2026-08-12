import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Matter, Resolution, Round
from hub.mcp_server.methods import mcp_get_matter_status
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
    rnd = db_session.scalar(select(Round))
    db_session.add(
        Resolution(matter_id=matter.id, source_round_id=rnd.id, version=1,
                   status="pending_review",
                   recommendation="采用方案 A", rationale="依据",
                   risks=[], divergences=[], cited_rounds=[1])
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="awaiting_decision")
    )
    db_session.commit()
    return {"matter": matter, "init": init, "alice": alice}


def test_resolution_stage_and_version_for_initiator(db_session, settings,
                                                    scenario):
    result = mcp_get_matter_status(db_session, settings,
                                   user_id=scenario["init"].id,
                                   matter_id=scenario["matter"].id)
    res = result["resolution"]
    assert res is not None
    assert res["status"] == "pending_review"
    assert res["version"] == 1
    assert res["cited_rounds"] == [1]
    assert res["created_at"].endswith("Z")
    assert res["decided_at"] is None


def test_resolution_visible_to_participant(db_session, settings, scenario):
    result = mcp_get_matter_status(db_session, settings,
                                   user_id=scenario["alice"].id,
                                   matter_id=scenario["matter"].id)
    assert result["resolution"]["status"] == "pending_review"


def test_resolution_after_decision(db_session, settings, scenario):
    from hub.domain.timeutil import utcnow

    db_session.execute(
        update(Resolution)
        .values(status="approved", version=2, final_text="最终",
                decided_at=utcnow())
    )
    db_session.commit()
    result = mcp_get_matter_status(db_session, settings,
                                   user_id=scenario["init"].id,
                                   matter_id=scenario["matter"].id)
    res = result["resolution"]
    assert res["status"] == "approved"
    assert res["version"] == 2
    assert res["decided_at"] is not None and res["decided_at"].endswith("Z")

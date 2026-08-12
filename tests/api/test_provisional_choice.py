import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.resolutions import (
    accept_provisional,
    continue_probing,
)
from hub.db.models import AuditEvent, Matter, Resolution, Round, RoundSummary
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
    """Matter in_progress (provisional pause) with a pending_review draft."""
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
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=[],
            convergence="provisionally_ready", generation_status="ok",
        )
    )
    db_session.add(
        Resolution(
            matter_id=matter.id, source_round_id=rnd.id, version=1,
            status="pending_review",
            recommendation="采用方案 A", rationale="依据",
            risks=[], divergences=["分歧"], cited_rounds=[1],
        )
    )
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init, "alice": alice}


def test_accept_flips_to_awaiting_decision_with_audit(db_session, scenario):
    accept_provisional(db_session, matter_id=scenario["matter"].id,
                       actor=scenario["init"])
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "matter_awaiting_decision"
    ]
    assert len(events) == 1
    assert events[0].detail["mode"] == "accept_provisional"
    assert events[0].detail["version"] == 1


def test_accept_is_idempotent_second_call_409(db_session, scenario):
    accept_provisional(db_session, matter_id=scenario["matter"].id,
                       actor=scenario["init"])
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        accept_provisional(db_session, matter_id=scenario["matter"].id,
                           actor=scenario["init"])
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "INVALID_STATE_TRANSITION"


def test_accept_rejected_for_participant(db_session, scenario):
    with pytest.raises(ApiError) as exc_info:
        accept_provisional(db_session, matter_id=scenario["matter"].id,
                           actor=scenario["alice"])
    assert exc_info.value.status_code == 403
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "forbidden_denied" in events


def test_accept_rejected_when_converged_already_awaiting(db_session, scenario):
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="awaiting_decision")
    )
    db_session.execute(
        update(RoundSummary)
        .where(RoundSummary.round_id == scenario["round"].id)
        .values(convergence="converged")
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        accept_provisional(db_session, matter_id=scenario["matter"].id,
                           actor=scenario["init"])
    assert exc_info.value.status_code == 409


def test_continue_probing_validates_without_writes(db_session, scenario):
    """只校验不改库：额度与新轮都在图的 gate_followup 节点内幂等完成。"""
    continue_probing(db_session, matter_id=scenario["matter"].id,
                     actor=scenario["init"])
    db_session.commit()
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "in_progress"
    assert matter.granted_extra_rounds == 0  # 授信不在这里发生
    assert db_session.scalar(select(Round).where(Round.round_number == 2)) is None


def test_continue_probing_guard_failures(db_session, scenario):
    with pytest.raises(ApiError) as exc_info:
        continue_probing(db_session, matter_id=scenario["matter"].id,
                         actor=scenario["alice"])
    assert exc_info.value.status_code == 403
    # 决议已终态 → 409
    db_session.execute(
        update(Resolution).values(status="approved", version=2)
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        continue_probing(db_session, matter_id=scenario["matter"].id,
                         actor=scenario["init"])
    assert exc_info.value.status_code == 409

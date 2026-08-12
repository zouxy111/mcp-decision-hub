import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.pipeline import (
    BLOCKED_REASON_DRAFT_FAILED,
    BLOCKED_REASON_SUMMARY_FAILED,
    find_interrupted_resolution_matter_ids,
    find_interrupted_round_ids,
)
from hub.db.models import AuditEvent, Matter, Resolution, Round
from hub.main import create_app
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
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init}


def _add_resolution(db_session, scenario, *, status, version=1):
    db_session.add(
        Resolution(
            matter_id=scenario["matter"].id,
            source_round_id=scenario["round"].id, version=version,
            status=status, recommendation="R", rationale="J",
            risks=[], divergences=[], cited_rounds=[1],
            final_text="R" if status in ("approved", "modified") else None,
        )
    )
    db_session.commit()


def test_reconcile_picks_up_decided_but_not_completed(db_session, scenario):
    _add_resolution(db_session, scenario, status="approved")
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="awaiting_decision")
    )
    db_session.commit()
    assert find_interrupted_resolution_matter_ids(db_session) == [
        scenario["matter"].id
    ]


def test_reconcile_ignores_pending_and_terminal_matters(db_session, scenario):
    # pending_review（等发起人）→ 不拾起
    _add_resolution(db_session, scenario, status="approved")
    db_session.execute(
        update(Resolution).values(status="pending_review")
    )
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="awaiting_decision")
    )
    db_session.commit()
    assert find_interrupted_resolution_matter_ids(db_session) == []
    # 已 completed → 不拾起
    db_session.execute(update(Resolution).values(status="approved"))
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="completed")
    )
    db_session.commit()
    assert find_interrupted_resolution_matter_ids(db_session) == []


def test_round_reconciler_excludes_matters_paused_at_gate(db_session, scenario):
    """rule (b) 演进：in_progress + 无活动轮 + 最新轮已有草案 → 停在闸门，
    不算分支中断，不重新 tick。"""
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="in_progress")
    )
    db_session.commit()
    # 无草案：分支/草案相位中断 → 拾起（M2 行为不变）
    assert scenario["round"].id in find_interrupted_round_ids(db_session)
    # 有草案：停在 provisional 闸门 → 不拾起
    _add_resolution(db_session, scenario, status="pending_review")
    assert scenario["round"].id not in find_interrupted_round_ids(db_session)


def test_startup_resume_completes_decided_matter(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """FR-24：决议已批准但进程死于传播前 → 重启后 reconciler 入队 resume，
    matter 完成且 checkpoint 与业务表一致。"""
    _add_resolution(db_session, scenario, status="approved")
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="awaiting_decision")
    )
    db_session.commit()
    app = create_app(settings, llm=make_fake_llm())

    def matter_status():
        with session_factory() as s:
            return s.get(Matter, scenario["matter"].id).status

    with TestClient(app):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if matter_status() == "completed":
                break
            time.sleep(0.1)
    assert matter_status() == "completed"
    with session_factory() as s:
        events = [r.event_type for r in s.scalars(select(AuditEvent)).all()]
        assert events.count("matter_completed") == 1


def test_continue_matter_retries_draft_failure_without_credit(
    db_session, scenario
):
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_DRAFT_FAILED)
    )
    db_session.commit()
    round_id = matter_svc.continue_matter(
        db_session, matter_id=scenario["matter"].id, actor=scenario["init"]
    )
    db_session.commit()
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "in_progress"
    assert matter.granted_extra_rounds == 0  # 重试不授额度
    assert round_id == scenario["round"].id
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "matter_continued"
    ]
    assert len(events) == 1
    assert events[0].detail["mode"] == "retry_draft"


def test_continue_matter_other_blocked_reasons_still_409(db_session, scenario):
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_SUMMARY_FAILED)
    )
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        matter_svc.continue_matter(
            db_session, matter_id=scenario["matter"].id, actor=scenario["init"]
        )
    assert exc_info.value.status_code == 409

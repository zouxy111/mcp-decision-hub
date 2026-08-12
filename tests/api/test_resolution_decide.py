import threading

import pytest
from sqlalchemy import func, select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.resolutions import decide_resolution, get_latest_resolution
from hub.db.models import AuditEvent, Matter, Resolution, Round
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
    """Matter awaiting_decision with a pending_review resolution v1."""
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
        Resolution(
            matter_id=matter.id, source_round_id=rnd.id, version=1,
            status="pending_review",
            recommendation="采用方案 A", rationale="依据",
            risks=["风险"], divergences=["分歧"], cited_rounds=[1],
        )
    )
    matter.status = "awaiting_decision"
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init,
            "alice": alice, "bob": bob}


def _decide(db_session, scenario, **overrides):
    kwargs = {
        "matter_id": scenario["matter"].id, "actor": scenario["init"],
        "decision": "approved", "expected_version": 1,
        "final_text": None, "rationale": None,
    }
    kwargs.update(overrides)
    return decide_resolution(db_session, **kwargs)


def test_approve_stores_composed_draft_as_final_text(db_session, scenario):
    _decide(db_session, scenario)
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.status == "approved"
    assert loaded.final_text is not None
    assert "采用方案 A" in loaded.final_text  # 平台草案即最终决议（7.4）
    assert loaded.decided_by == scenario["init"].id
    assert loaded.decided_at is not None
    assert loaded.version == 2  # 拍板后 version 递增
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "resolution_decided"
    ]
    assert len(events) == 1
    assert events[0].detail["decision"] == "approved"
    assert events[0].detail["version"] == 1  # 拍板时所见版本


def test_modified_requires_and_stores_final_text_and_rationale(
    db_session, scenario
):
    with pytest.raises(ApiError) as exc_info:
        _decide(db_session, scenario, decision="modified",
                final_text=None, rationale="理由")
    assert exc_info.value.status_code == 422
    with pytest.raises(ApiError):
        _decide(db_session, scenario, decision="modified",
                final_text="最终文本", rationale=None)
    _decide(db_session, scenario, decision="modified",
            final_text="最终文本", rationale="修改理由")
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.status == "modified"
    assert loaded.final_text == "最终文本"
    assert loaded.decision_rationale == "修改理由"
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "resolution_decided"
    ]
    assert events[-1].detail["rationale"] == "修改理由"  # 理由入审计（截断 500）


def test_reject_requires_rationale(db_session, scenario):
    with pytest.raises(ApiError) as exc_info:
        _decide(db_session, scenario, decision="rejected",
                rationale=None)
    assert exc_info.value.status_code == 422
    _decide(db_session, scenario, decision="rejected",
            rationale="证据不足，再议")
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.status == "rejected"
    assert loaded.decision_rationale == "证据不足，再议"
    assert loaded.final_text is None


def test_participant_cannot_decide(db_session, scenario):
    with pytest.raises(ApiError) as exc_info:
        _decide(db_session, scenario, actor=scenario["alice"])
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "FORBIDDEN_SCOPE"
    events = [r.event_type for r in db_session.scalars(select(AuditEvent)).all()]
    assert "forbidden_denied" in events
    db_session.expire_all()
    assert db_session.scalar(select(Resolution)).status == "pending_review"


def test_decide_requires_awaiting_decision(db_session, scenario):
    db_session.get(Matter, scenario["matter"].id).status = "in_progress"
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        _decide(db_session, scenario)
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "INVALID_STATE_TRANSITION"


def test_stale_version_returns_409_conflict_without_write(db_session, scenario):
    with pytest.raises(ApiError) as exc_info:
        _decide(db_session, scenario, expected_version=99)
    err = exc_info.value
    assert err.status_code == 409
    assert err.error_code == "RESOLUTION_VERSION_CONFLICT"
    assert err.details["current_version"] == 1
    assert err.details["current_status"] == "pending_review"
    db_session.expire_all()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.status == "pending_review"  # 不写入
    assert loaded.version == 1


def test_second_decision_on_terminal_returns_invalid_state(db_session, scenario):
    _decide(db_session, scenario)
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        # approved 后 version=2；携带新 version 重拍 → 终态拒绝
        _decide(db_session, scenario, expected_version=2)
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "INVALID_STATE_TRANSITION"
    db_session.expire_all()
    assert db_session.scalar(
        select(func.count()).select_from(Resolution)
    ) == 1


def test_concurrent_decisions_only_one_wins(session_factory, scenario):
    """场景 9/17：两个线程并发拍板同一 version，只有一个成功，另一个
    409 RESOLUTION_VERSION_CONFLICT，决议不被覆盖。"""
    results = {"ok": 0, "conflict": 0, "other": []}

    def worker():
        with session_factory() as session:
            init = session.get(type(scenario["init"]), scenario["init"].id)
            try:
                decide_resolution(
                    session, matter_id=scenario["matter"].id, actor=init,
                    decision="approved", expected_version=1,
                    final_text=None, rationale=None,
                )
                session.commit()
                results["ok"] += 1
            except ApiError as e:
                session.rollback()
                if e.error_code == "RESOLUTION_VERSION_CONFLICT":
                    results["conflict"] += 1
                else:
                    results["other"].append(e.error_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["ok"] == 1
    assert results["conflict"] == 1
    assert results["other"] == []
    with session_factory() as session:
        res = session.scalar(select(Resolution))
        assert res.status == "approved"
        assert res.version == 2


def test_get_latest_resolution_returns_highest_version(db_session, scenario):
    assert get_latest_resolution(
        db_session, matter_id=scenario["matter"].id
    ).version == 1
    assert get_latest_resolution(db_session, matter_id="mat_nope") is None

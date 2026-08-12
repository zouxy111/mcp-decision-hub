"""PRD 7.5: 轮次上限 blocked 时发起人可直接要求生成决议草案。"""

import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.pipeline import (
    BLOCKED_REASON_ROUND_LIMIT,
    BLOCKED_REASON_SUMMARY_FAILED,
)
from hub.api.resolutions import draft_resolution_from_blocked
from hub.db.models import AuditEvent, Matter, Resolution, Round, RoundSummary
from hub.llm.client import LLMError
from tests.conftest import make_user

DRAFT_PAYLOAD = {
    "recommendation": "采用方案 A", "rationale": "依据",
    "risks": ["风险"], "divergences": [], "cited_rounds": [1],
}


@pytest.fixture()
def app_llm(make_fake_llm):
    """client fixture 用：注入脚本化 FakeLLM，避免 app_llm=None 让 create_app
    构造真实 DeepSeekClient（M2 既有模式，参照
    tests/web/test_matter_detail_summaries.py 顶部）。"""
    return make_fake_llm([DRAFT_PAYLOAD])


@pytest.fixture()
def scenario(db_session):
    """Matter blocked at the round limit; round 1 closed with a continue
    summary (PRD 7.5 入口的前置形态)。"""
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=1, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    rnd = db_session.scalar(select(Round))
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=["未决"],
            convergence="continue", generation_status="ok",
        )
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_ROUND_LIMIT)
    )
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init, "alice": alice}


def _events(db_session, event_type):
    return [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == event_type
    ]


def test_participant_cannot_draft_from_blocked(db_session, scenario, make_fake_llm):
    llm = make_fake_llm([DRAFT_PAYLOAD])
    with pytest.raises(ApiError) as exc_info:
        draft_resolution_from_blocked(
            db_session, matter_id=scenario["matter"].id,
            actor=scenario["alice"], llm=llm)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "FORBIDDEN_SCOPE"
    assert len(_events(db_session, "forbidden_denied")) == 1
    assert len(llm.calls) == 0
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 0


def test_non_round_limit_blocked_rejected(db_session, scenario, make_fake_llm):
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(blocked_reason=BLOCKED_REASON_SUMMARY_FAILED)
    )
    db_session.commit()
    llm = make_fake_llm([DRAFT_PAYLOAD])
    with pytest.raises(ApiError) as exc_info:
        draft_resolution_from_blocked(
            db_session, matter_id=scenario["matter"].id,
            actor=scenario["init"], llm=llm)
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "INVALID_STATE_TRANSITION"
    assert len(llm.calls) == 0


def test_success_creates_draft_and_flips_to_awaiting_decision(
    db_session, scenario, make_fake_llm
):
    llm = make_fake_llm([DRAFT_PAYLOAD])
    res = draft_resolution_from_blocked(
        db_session, matter_id=scenario["matter"].id,
        actor=scenario["init"], llm=llm)
    db_session.commit()
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "awaiting_decision"
    assert matter.blocked_reason is None
    assert res.version == 1
    assert res.status == "pending_review"
    assert res.source_round_id == scenario["round"].id
    assert res.recommendation == "采用方案 A"
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema_name"] == "resolution_draft"
    assert "共识" in llm.calls[0]["user_prompt"]  # 全部轮次 ok 摘要入 prompt
    drafted = _events(db_session, "resolution_drafted")
    assert len(drafted) == 1
    assert drafted[0].detail["trigger"] == "manual_from_blocked"
    awaiting = _events(db_session, "matter_awaiting_decision")
    assert len(awaiting) == 1
    assert awaiting[0].detail["mode"] == "manual_from_blocked"


def test_llm_failure_keeps_blocked_with_visible_error(
    db_session, scenario, make_fake_llm
):
    llm = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    with pytest.raises(ApiError) as exc_info:
        draft_resolution_from_blocked(
            db_session, matter_id=scenario["matter"].id,
            actor=scenario["init"], llm=llm)
    err = exc_info.value
    assert err.status_code == 503
    assert err.error_code == "SERVICE_UNAVAILABLE"
    assert "LLM_TIMEOUT" in err.message
    assert "已重试 3 次" in err.message
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_ROUND_LIMIT  # 原因不变
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 0
    failed = _events(db_session, "llm_failed")
    assert len(failed) == 1
    assert failed[0].detail["trigger"] == "manual_from_blocked"
    assert failed[0].detail["error_code"] == "LLM_TIMEOUT"


def test_repeat_trigger_is_idempotent_409(db_session, scenario, make_fake_llm):
    llm = make_fake_llm([DRAFT_PAYLOAD])
    draft_resolution_from_blocked(
        db_session, matter_id=scenario["matter"].id,
        actor=scenario["init"], llm=llm)
    db_session.commit()
    with pytest.raises(ApiError) as exc_info:
        draft_resolution_from_blocked(
            db_session, matter_id=scenario["matter"].id,
            actor=scenario["init"], llm=llm)
    assert exc_info.value.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1
    assert len(llm.calls) == 1  # 未重复调 LLM


def test_detail_button_and_post_flow(client, db_session, scenario):
    client.post("/login", data={"username": "init", "password": "pw-123456"},
                follow_redirects=False)
    resp = client.get(f"/matters/{scenario['matter'].id}")
    assert resp.status_code == 200
    assert "直接生成决议草案" in resp.text
    assert "继续（+1 轮）" in resp.text  # 两按钮并列
    resp = client.post(f"/matters/{scenario['matter'].id}/draft-resolution",
                       follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "awaiting_decision"
    res = db_session.scalar(select(Resolution))
    assert res is not None
    assert res.status == "pending_review"
    assert res.version == 1

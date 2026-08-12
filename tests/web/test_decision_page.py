import time

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Matter, Resolution, Round, RoundSummary
from tests.conftest import make_user

DRAFT = {
    "recommendation": "采用方案 A", "rationale": "依据",
    "risks": ["风险一"], "divergences": ["分歧一"], "cited_rounds": [1],
}


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    outsider = make_user(db_session, "outsider", password="pw-123456")
    db_session.commit()
    return {"init": init, "alice": alice, "bob": bob, "outsider": outsider}


@pytest.fixture()
def scenario(db_session, users):
    """Matter awaiting_decision with pending_review draft v1."""
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="选型", goal="定方案",
        background="背景",
        participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=users["init"])
    rnd = db_session.scalar(select(Round))
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=[],
            convergence="converged", generation_status="ok",
        )
    )
    db_session.add(
        Resolution(matter_id=matter.id, source_round_id=rnd.id, version=1,
                   status="pending_review", **DRAFT)
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="awaiting_decision")
    )
    db_session.commit()
    return matter


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def test_decision_page_initiator_sees_draft_and_form(client, scenario):
    _login(client, "init")
    resp = client.get(f"/matters/{scenario.id}/decision")
    assert resp.status_code == 200
    assert "采用方案 A" in resp.text
    assert "依据" in resp.text
    assert "风险一" in resp.text
    assert "分歧一" in resp.text
    assert "版本：1" in resp.text
    assert 'name="decision"' in resp.text
    assert 'name="version"' in resp.text


def test_decision_page_participant_is_readonly(client, scenario):
    _login(client, "alice")
    resp = client.get(f"/matters/{scenario.id}/decision")
    assert resp.status_code == 200
    assert "采用方案 A" in resp.text  # 草案可见
    assert 'name="decision"' not in resp.text  # 但无表单


def test_decision_page_outsider_404(client, scenario):
    _login(client, "outsider")
    resp = client.get(f"/matters/{scenario.id}/decision")
    assert resp.status_code == 404


def test_decision_page_without_resolution_redirects(client, db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B",
        participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}/decision", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/matters/{matter.id}"


def test_post_approve_redirects_and_completes(
    client, db_session, session_factory, scenario
):
    _login(client, "init")
    resp = client.post(
        f"/matters/{scenario.id}/decision",
        data={"action": "decide", "decision": "approved", "version": "1",
              "final_text": "", "rationale": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # resume 经 resume_queue 异步传播（app 默认 llm 无 key，approve 不调 LLM）
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with session_factory() as s:
            if s.get(Matter, scenario.id).status == "completed":
                break
        time.sleep(0.1)
    with session_factory() as s:
        matter = s.get(Matter, scenario.id)
        assert matter.status == "completed"
        res = s.scalar(select(Resolution))
        assert res.status == "approved"
        assert res.version == 2
        assert "采用方案 A" in res.final_text


def test_post_stale_version_shows_reload_message(client, db_session, users,
                                                scenario):
    """场景 17 / FR-26：陈旧版本拍板 → 409 页面提示重新加载后再操作。"""
    from hub.api.resolutions import decide_resolution

    decide_resolution(db_session, matter_id=scenario.id, actor=users["init"],
                      decision="approved", expected_version=1)
    db_session.commit()
    _login(client, "init")
    resp = client.post(
        f"/matters/{scenario.id}/decision",
        data={"action": "decide", "decision": "rejected", "version": "1",
              "final_text": "", "rationale": "换个方案"},
    )
    assert resp.status_code == 409
    assert "请重新加载后再操作" in resp.text
    db_session.expire_all()
    res = db_session.scalar(select(Resolution))
    assert res.status == "approved"  # 不被覆盖


def test_post_reject_without_rationale_422(client, scenario):
    _login(client, "init")
    resp = client.post(
        f"/matters/{scenario.id}/decision",
        data={"action": "decide", "decision": "rejected", "version": "1",
              "final_text": "", "rationale": ""},
    )
    assert resp.status_code == 422
    assert "驳回必须填写理由" in resp.text


def test_post_participant_forbidden(client, db_session, scenario):
    _login(client, "alice")
    resp = client.post(
        f"/matters/{scenario.id}/decision",
        data={"action": "decide", "decision": "approved", "version": "1",
              "final_text": "", "rationale": ""},
    )
    assert resp.status_code == 403
    db_session.expire_all()
    assert db_session.scalar(select(Resolution)).status == "pending_review"


def test_detail_page_shows_decision_card(client, scenario):
    _login(client, "init")
    resp = client.get(f"/matters/{scenario.id}")
    assert resp.status_code == 200
    assert "决议草案" in resp.text
    assert f"/matters/{scenario.id}/decision" in resp.text
    assert "下一阶段开放拍板" not in resp.text  # 占位文案已替换


def test_provisional_page_shows_two_choices(client, db_session, scenario):
    db_session.execute(
        update(RoundSummary).values(convergence="provisionally_ready")
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario.id)
        .values(status="in_progress")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{scenario.id}/decision")
    assert resp.status_code == 200
    assert "继续追问" in resp.text
    assert "进入拍板" in resp.text
    assert 'value="continue_probing"' in resp.text
    assert 'value="accept"' in resp.text


def test_post_accept_enters_awaiting_decision(client, db_session, scenario):
    db_session.execute(
        update(RoundSummary).values(convergence="provisionally_ready")
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario.id)
        .values(status="in_progress")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.post(
        f"/matters/{scenario.id}/decision",
        data={"action": "accept", "decision": "", "version": "1",
              "final_text": "", "rationale": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(Matter, scenario.id).status == "awaiting_decision"

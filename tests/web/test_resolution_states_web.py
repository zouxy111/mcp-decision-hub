import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_DRAFT_FAILED,
    BLOCKED_REASON_ROUND_LIMIT,
)
from hub.db.models import Matter, Resolution, Round, RoundSummary
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    return {"init": init, "alice": alice, "bob": bob}


@pytest.fixture()
def matter(db_session, users):
    m = matter_svc.create_matter(
        db_session, initiator=users["init"], title="选型", goal="定方案",
        background="背景",
        participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=m.id, actor=users["init"])
    db_session.commit()
    return m


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def _close_round_ready(db_session, matter):
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=["共识"], divergences=[], blind_spots=[],
            open_questions=[], convergence="converged", generation_status="ok",
        )
    )
    db_session.commit()
    return rnd


def test_draft_in_progress_shows_processing(client, db_session, matter):
    """已收敛、草案未落库（管线处理中）→ 展示处理中与下一步动作。"""
    _close_round_ready(db_session, matter)
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "正在生成决议草案" in resp.text
    assert "请稍后刷新" in resp.text


def test_draft_failure_shows_error_and_retry_button(client, db_session, matter):
    _close_round_ready(db_session, matter)
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked",
                blocked_reason=f"{BLOCKED_REASON_DRAFT_FAILED}：LLM_TIMEOUT（已重试 3 次）")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "决议草案生成失败" in resp.text
    assert "LLM_TIMEOUT" in resp.text
    assert "已重试 3 次" in resp.text
    assert "重试生成草案" in resp.text
    assert f'/matters/{matter.id}/continue' in resp.text


def test_draft_retry_continue_returns_in_progress(client, db_session, matter):
    _close_round_ready(db_session, matter)
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_DRAFT_FAILED)
    )
    db_session.commit()
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/continue", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    m = db_session.get(Matter, matter.id)
    assert m.status == "in_progress"
    assert m.granted_extra_rounds == 0


def test_round_limit_continue_button_label_unchanged(client, db_session, matter):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason=BLOCKED_REASON_ROUND_LIMIT)
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "继续（+1 轮）" in resp.text


def test_completed_hides_action_buttons(client, db_session, matter):
    rnd = _close_round_ready(db_session, matter)
    db_session.add(
        Resolution(matter_id=matter.id, source_round_id=rnd.id, version=2,
                   status="approved", recommendation="R", rationale="J",
                   risks=[], divergences=[], cited_rounds=[1],
                   final_text="最终决议文本")
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="completed")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "最终决议文本" in resp.text
    assert "/continue" not in resp.text
    assert "/start" not in resp.text
    assert "提交拍板" not in resp.text

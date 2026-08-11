import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Matter, Round, RoundSummary
from tests.conftest import make_user


@pytest.fixture()
def app_llm(make_fake_llm):
    return make_fake_llm([{"questions": ["追问一？"]}])


OK_SUMMARY = {
    "consensus_points": ["都认可方向 X"],
    "divergences": ["成本口径不一致"],
    "blind_spots": ["运维成本无人覆盖"],
    "open_questions": ["进度如何保证？"],
    "convergence": "continue",
}


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    return init, alice, bob


@pytest.fixture()
def matter(db_session, users):
    init, alice, bob = users
    m = matter_svc.create_matter(
        db_session, initiator=init, title="选型", goal="定方案", background="背景",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=m.id, actor=init)
    db_session.commit()
    return m


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def _round1(db_session, matter):
    return db_session.scalar(select(Round).where(Round.matter_id == matter.id))


def test_detail_shows_summary_blocks_and_badge_for_initiator(
    client, db_session, matter
):
    rnd = _round1(db_session, matter)
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.add(RoundSummary(round_id=rnd.id, matter_id=matter.id, **OK_SUMMARY,
                                generation_status="ok"))
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    for text in ("共识", "分歧", "盲区", "未解决问题",
                 "都认可方向 X", "成本口径不一致", "运维成本无人覆盖",
                 "进度如何保证？", "continue"):
        assert text in resp.text


def test_participant_sees_summary_but_not_others_answers(
    client, db_session, matter
):
    rnd = _round1(db_session, matter)
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.add(RoundSummary(round_id=rnd.id, matter_id=matter.id, **OK_SUMMARY,
                                generation_status="ok"))
    db_session.commit()
    _login(client, "alice")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "都认可方向 X" in resp.text  # 摘要参与人可见（FR-07）


def test_blocked_shows_reason_and_error_details(client, db_session, matter):
    rnd = _round1(db_session, matter)
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="failed"))
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason="摘要生成失败（LLM 重试耗尽）")
    )
    db_session.add(
        RoundSummary(round_id=rnd.id, matter_id=matter.id,
                     consensus_points=[], divergences=[], blind_spots=[],
                     open_questions=[], convergence=None,
                     generation_status="failed",
                     error_code="LLM_TIMEOUT", retry_count=3)
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "摘要生成失败（LLM 重试耗尽）" in resp.text
    assert "LLM_TIMEOUT" in resp.text
    assert "3" in resp.text


def test_rounds_counter_and_limit_highlight(client, db_session, matter):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(max_rounds=1)  # 已有第 1 轮 → 达上限
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "已达上限" in resp.text
    assert "1" in resp.text  # 累计轮次/上限均渲染


def test_awaiting_decision_shows_placeholder(client, db_session, matter):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="awaiting_decision")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "等待决议（下一阶段开放拍板）" in resp.text


def test_continue_button_only_for_initiator_at_round_limit(
    client, db_session, matter
):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason="达到轮次上限")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "继续（+1 轮）" in resp.text

    _login(client, "alice")
    resp = client.get(f"/matters/{matter.id}")
    assert "继续（+1 轮）" not in resp.text


def test_continue_button_hidden_for_other_blocked_reasons(
    client, db_session, matter
):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason="本轮无有效输出")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "继续（+1 轮）" not in resp.text


def test_continue_route_grants_credit_and_redrives(
    client, db_session, session_factory, matter, app_llm
):
    """发起人点击继续 → +1 额度 → 后台重驱动生成新一轮（端到端走队列）。"""
    import time

    from sqlalchemy import func, select

    from hub.db.models import Round, RoundSummary

    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.execute(update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason="达到轮次上限",
                max_rounds=1)
    )
    db_session.add(
        RoundSummary(round_id=rnd.id, matter_id=matter.id,
                     consensus_points=["共识"], divergences=["分歧"],
                     blind_spots=[], open_questions=["未决"],
                     convergence="continue", generation_status="ok")
    )
    db_session.commit()
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/continue", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    reloaded = db_session.get(Matter, matter.id)
    assert reloaded.granted_extra_rounds == 1

    def round_count() -> int:
        # 每次轮询开新会话：WAL 下长事务读不到新提交的快照
        with session_factory() as s:
            return s.scalar(select(func.count()).select_from(Round))

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if round_count() == 2:
            break
        time.sleep(0.1)
    assert round_count() == 2
    with session_factory() as s:
        assert s.get(Matter, matter.id).status == "collecting"


def test_continue_route_forbidden_for_participant(client, db_session, matter):
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="blocked", blocked_reason="达到轮次上限")
    )
    db_session.commit()
    _login(client, "alice")
    resp = client.post(f"/matters/{matter.id}/continue", follow_redirects=False)
    assert resp.status_code == 403
    db_session.expire_all()
    assert db_session.get(Matter, matter.id).granted_extra_rounds == 0

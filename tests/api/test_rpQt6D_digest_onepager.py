"""`get_digest` = 一页纸现状（PRD-03）：`current_round` / `decision` / `open_items`。

背景：`873ff8d` 落地时把口径简化成「最新 ok 摘要 + 状态 + 收敛」，
`decision`（最新决议 / 决议草案）**整节缺失** —— 而那正是「这个事项进行到哪了」
最该回答的一节。本文件按 owner 口径（09-14 定、09-16 复核「不变」）把它钉住。

同时钉住 PRD-03 的边界：**不含** `user_id` / `confidence` / 逐人立场。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError, error_payload
from hub.api.tokens import issue_token
from hub.db.models import Resolution, Round, RoundSummary
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import DigestOut
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def digest_scenario(client, db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    outsider = make_user(db_session, "outsider")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.add(RoundSummary(
        round_id=rnd.id, matter_id=matter.id,
        consensus_points=["共识X"], divergences=["分歧Y"], blind_spots=[],
        open_questions=["还差预算口径"], convergence="continue",
        generation_status="ok",
    ))
    db_session.commit()

    headers = {}
    for name, user in (("init", init), ("alice", alice),
                       ("outsider", outsider)):
        _t, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()
    return {"matter": matter, "round": rnd, "init": init, "alice": alice,
            "outsider": outsider, "init_headers": headers["init"],
            "outsider_headers": headers["outsider"]}


def test_三节齐备且可被契约校验(client, db_session, settings, digest_scenario):
    s = digest_scenario
    body = methods.mcp_get_digest(db_session, settings,
                                  user_id=s["init"].id,
                                  matter_id=s["matter"].id)

    DigestOut.model_validate(body)                       # 出参过契约
    assert body["current_round"]["round_number"] == 1
    assert body["current_round"]["status"] == s["round"].status
    assert body["open_items"] == ["还差预算口径"]
    # 摘要五字段仍在（既有能力不缩）
    assert body["consensus_points"] == ["共识X"]
    assert body["convergence"] == "continue"


def test_无决议时decision键仍在值为None(client, db_session, settings,
                                       digest_scenario):
    s = digest_scenario
    body = methods.mcp_get_digest(db_session, settings,
                                  user_id=s["init"].id,
                                  matter_id=s["matter"].id)
    assert "decision" in body and body["decision"] is None


def test_pending_review时给草案摘要并标注状态(client, db_session, settings,
                                            digest_scenario):
    """PRD-03 原话：「pending_review 时给草案，标注状态」。"""
    s = digest_scenario
    db_session.add(Resolution(
        matter_id=s["matter"].id, source_round_id=s["round"].id, version=1,
        status="pending_review", recommendation="采用方案 A",
        rationale="依据", risks=["风险"], divergences=["分歧"],
        cited_rounds=[1],
    ))
    db_session.commit()

    body = methods.mcp_get_digest(db_session, settings, user_id=s["init"].id,
                                  matter_id=s["matter"].id)
    decision = body["decision"]
    assert decision["status"] == "pending_review"
    assert decision["final"] is False
    assert decision["text"] == "采用方案 A"      # 草案阶段给 recommendation
    assert decision["version"] == 1
    assert decision["cited_rounds"] == [1]


def test_已定稿时给定稿正文(client, db_session, settings, digest_scenario):
    s = digest_scenario
    db_session.add(Resolution(
        matter_id=s["matter"].id, source_round_id=s["round"].id, version=1,
        status="approved", recommendation="草案原文", rationale="依据",
        risks=[], divergences=[], cited_rounds=[1],
        final_text="定稿正文", decided_at=None,
    ))
    db_session.commit()

    body = methods.mcp_get_digest(db_session, settings, user_id=s["init"].id,
                                  matter_id=s["matter"].id)
    assert body["decision"]["final"] is True
    assert body["decision"]["text"] == "定稿正文"


def test_无决议时decision键仍在_且不夹带私有字段(client, db_session, settings,
                                                digest_scenario):
    """PRD-03 验收第 2 条：手工塞一个 `user_id` 进去必须炸（契约钉死白名单）。"""
    s = digest_scenario
    body = methods.mcp_get_digest(db_session, settings, user_id=s["init"].id,
                                  matter_id=s["matter"].id)
    for banned in ("user_id", "confidence", "confidence_band"):
        assert banned not in str(body)

    with pytest.raises(Exception):
        DigestOut.model_validate({**body, "user_id": 7})

    assert "confidence" not in str(body)
    assert "user_id" not in str(body)


def test_非成员404不泄露存在性(client, db_session, settings, digest_scenario):
    s = digest_scenario
    resp = client.get(f"/api/items/{s['matter'].id}/digest",
                      headers=s["outsider_headers"])
    assert resp.status_code == 404
    rest_error = resp.json()
    with pytest.raises(ApiError) as exc:
        methods.mcp_get_digest(db_session, settings, user_id=s["outsider"].id,
                               matter_id=s["matter"].id)
    mcp_error = error_payload(exc.value)
    assert rest_error["error_code"] == mcp_error["error_code"]
    assert rest_error["message"] == mcp_error["message"]


def test_阻塞原因并入未决开口(client, db_session, settings, digest_scenario):
    s = digest_scenario
    matter = db_session.get(type(s["matter"]), s["matter"].id)
    matter.blocked_reason = "参与人甲逾期未交"
    db_session.commit()

    body = methods.mcp_get_digest(db_session, settings, user_id=s["init"].id,
                                  matter_id=s["matter"].id)
    assert body["open_items"] == ["还差预算口径", "参与人甲逾期未交"]


def test_current_round为当前已开启的最新轮次(client, db_session, settings,
                                             digest_scenario):
    """裁定 3（2026-09-17）：current_round = 当前已开启的最新轮次——
    第 2 轮一开即显示 2，不等出摘要（owner 16:09 定语义并明言「行为不符
    就算 bug」；柠檬果实测旧行为：第 2 轮已开无摘要时仍报 1）。

    注意区分：摘要五字段仍来自最新一轮 ok 摘要（第 1 轮）——一页纸的
    内容基于摘要不变，变的只是 current_round 这一节的语义。
    """
    s = digest_scenario
    db_session.add(Round(matter_id=s["matter"].id, round_number=2,
                         status="open"))
    db_session.commit()

    body = methods.mcp_get_digest(db_session, settings,
                                  user_id=s["init"].id,
                                  matter_id=s["matter"].id)

    assert body["current_round"]["round_number"] == 2
    assert body["current_round"]["status"] == "open"
    assert body["consensus_points"] == ["共识X"]  # 摘要仍取自第 1 轮

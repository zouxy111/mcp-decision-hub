"""rpQt6D · 端点缺口：`POST /api/items` / `GET /api/items/{id}/summary`
/ `GET /api/items/{id}/digest`。

与 MCP 工具**同源**（`test_rest_fallback.py` D3）：两侧调用同一个
`hub.mcp_server.methods.mcp_*` 函数，返回经同一组 `hub.schemas.mcp_outputs`
契约。

为什么这条不新写业务逻辑：三个服务层实现（`mcp_declare_item` /
`mcp_get_summary` / `mcp_get_digest`）早已存在，缺的只是 HTTP 出口。所以
本切片只做「把已有服务接到第二个出口上」，并钉住两个出口不会各写一份。
"""

import pytest
from sqlalchemy import delete, select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError, error_payload
from hub.api.tokens import issue_token
from hub.db.models import Matter, Round, RoundSummary
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import DeclareItemOut, RoundSummaryOut
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def items_scenario(client, db_session):
    """一个已开跑的事项 + 一条 ok 摘要；发起人 / 参与人 / 局外人各持 token。"""
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
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.add(
        RoundSummary(
            round_id=rnd.id, matter_id=matter.id,
            consensus_points=["共识一"], divergences=["分歧一"],
            blind_spots=["盲点一"], open_questions=["待答一"],
            convergence="continue", generation_status="ok",
        )
    )
    db_session.commit()

    headers = {}
    for name, user in (("init", init), ("alice", alice),
                       ("outsider", outsider)):
        _t, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()
    return {
        "matter": matter, "round": rnd,
        "init": init, "alice": alice, "bob": bob, "outsider": outsider,
        "init_headers": headers["init"], "alice_headers": headers["alice"],
        "outsider_headers": headers["outsider"],
    }


def test_声明事项经REST返回契约且带降级标头(client, db_session, items_scenario):
    """新端点的存在性 + 形状 + 通道可观测性（D4）。"""
    s = items_scenario
    resp = client.post(
        "/api/items",
        json={"title": "新事项", "question": "选哪个？",
              "participant_ids": [s["alice"].id, s["bob"].id]},
        headers=s["init_headers"],
    )

    assert resp.status_code == 200, resp.json()
    assert resp.headers[CHANNEL] == "rest"

    body = resp.json()
    DeclareItemOut.model_validate(body)  # 过同一组契约
    assert body["item_version"] == 1
    assert body["irreversible"] is False
    db_session.expire_all()
    assert db_session.get(Matter, body["matter_id"]) is not None


def test_摘要两通道字段集合逐字段相同(client, db_session, settings,
                                      items_scenario):
    """D2 + D3：同一 methods 函数、同一契约，两侧返回逐字段相同。"""
    s = items_scenario
    rest_body = client.get(
        f"/api/items/{s['matter'].id}/summary", headers=s["init_headers"]
    ).json()
    mcp_body = methods.mcp_get_summary(
        db_session, settings, user_id=s["init"].id, matter_id=s["matter"].id
    )

    assert rest_body == mcp_body
    RoundSummaryOut.model_validate(rest_body)


def test_一页纸两通道字段集合逐字段相同(client, db_session, settings,
                                       items_scenario):
    """两通道逐字段相等（比只校验契约更强：契约存在也拦不住两侧各写一份）。"""
    s = items_scenario
    rest_body = client.get(
        f"/api/items/{s['matter'].id}/digest", headers=s["init_headers"]
    ).json()
    mcp_body = methods.mcp_get_digest(
        db_session, settings, user_id=s["init"].id, matter_id=s["matter"].id
    )

    assert rest_body == mcp_body
    assert set(rest_body) >= {"matter_id", "status", "current_round", "decision"}


@pytest.mark.parametrize("path_fn,mcp_fn", [
    (lambda mid: f"/api/items/{mid}/summary", "mcp_get_summary"),
    (lambda mid: f"/api/items/{mid}/digest", "mcp_get_digest"),
])
def test_局外人读被拒_两通道错误形状一致(client, db_session, settings,
                                          items_scenario, path_fn, mcp_fn):
    """D6 + 读侧成员闸门：非成员一律 404「事项不存在」。

    为什么这条必须钉：`matters.get_matter_for_user` 对非成员**返回 None 而不是
    抛错**，调用方漏判就等于闸门不存在。原 `mcp_get_digest` 正是这么漏的——
    任何持令牌用户都能读到别人事项的一页纸。`mcp_get_summary` 干脆没调闸门。
    """
    s = items_scenario
    resp = client.get(path_fn(s["matter"].id), headers=s["outsider_headers"])
    assert resp.status_code == 404, resp.json()
    rest_error = resp.json()

    with pytest.raises(ApiError) as exc:
        getattr(methods, mcp_fn)(
            db_session, settings, user_id=s["outsider"].id,
            matter_id=s["matter"].id,
        )
    mcp_error = error_payload(exc.value)

    assert rest_error["error_code"] == mcp_error["error_code"] \
        == "RESOURCE_NOT_FOUND"
    assert rest_error["message"] == mcp_error["message"] == "事项不存在"


def test_成员仍能正常读_防过度拒绝(client, items_scenario):
    """对照：闸门加紧了，成员（发起人 + 参与人）必须照常读到。"""
    s = items_scenario
    for headers in (s["init_headers"], s["alice_headers"]):
        assert client.get(f"/api/items/{s['matter'].id}/summary",
                          headers=headers).status_code == 200
        assert client.get(f"/api/items/{s['matter'].id}/digest",
                          headers=headers).status_code == 200


def test_无摘要时两通道错误形状一致(client, db_session, settings,
                                    items_scenario):
    """404 分支同样逐字段一致 —— 证明两侧是同一份实现而非各自兜底。"""
    s = items_scenario
    db_session.execute(delete(RoundSummary))
    db_session.commit()

    resp = client.get(f"/api/items/{s['matter'].id}/summary",
                      headers=s["init_headers"])
    assert resp.status_code == 404
    rest_error = resp.json()

    with pytest.raises(ApiError) as exc:
        methods.mcp_get_summary(
            db_session, settings, user_id=s["init"].id,
            matter_id=s["matter"].id,
        )
    mcp_error = error_payload(exc.value)

    assert rest_error["error_code"] == mcp_error["error_code"] \
        == "RESOURCE_NOT_FOUND"
    assert rest_error["message"] == mcp_error["message"]

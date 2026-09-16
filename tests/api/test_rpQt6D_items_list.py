"""rpQt6D · 端点缺口：`GET /api/items`（含 `?participant=me&state=open`）。

与 MCP 工具 `list_items` **同源**：两侧调用同一个
`hub.mcp_server.methods.mcp_list_items`，返回同一组
`hub.schemas.mcp_outputs.ItemListOut` 契约。

可见性只由调用方身份派生，不接受他人 user_id —— 所以本文件不需要「越权」
用例，只需要证明「筛选生效」与「看不到别人的」。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError, error_payload
from hub.api.tokens import issue_token
from hub.db.models import Matter
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import ItemListOut
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def list_scenario(client, db_session):
    """三件事项：init 发起 A/C（C 已终态）、alice 发起 B（init 是 B 的参与人）。"""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    outsider = make_user(db_session, "outsider")

    def _create(initiator, title):
        return matter_svc.create_matter(
            db_session, initiator=initiator, title=title, goal="G",
            background="B", participant_ids=[alice.id, bob.id]
            if initiator is init else [init.id, bob.id],
            initiator_participates=False, timeout_seconds=3600,
            max_rounds=10, draft_questions=["Q1?"],
        )

    a = _create(init, "A")
    b = _create(alice, "B")
    c = _create(init, "C")
    c.status = "completed"  # 终态
    db_session.commit()

    headers = {}
    for name, user in (("init", init), ("alice", alice),
                       ("outsider", outsider)):
        _t, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()
    return {
        "a": a, "b": b, "c": c,
        "init": init, "alice": alice, "outsider": outsider,
        "init_headers": headers["init"], "alice_headers": headers["alice"],
        "outsider_headers": headers["outsider"],
    }


def _ids(body):
    return [item["matter_id"] for item in body["items"]]


def test_列表返回本人可见事项且带降级标头(client, list_scenario):
    s = list_scenario
    resp = client.get("/api/items", headers=s["init_headers"])

    assert resp.status_code == 200, resp.json()
    assert resp.headers[CHANNEL] == "rest"
    body = resp.json()
    ItemListOut.model_validate(body)  # 过同一组契约
    assert set(_ids(body)) == {s["a"].id, s["b"].id, s["c"].id}


def test_看不见的事项不出现(client, list_scenario):
    """对照：outsider 与三件事项都无关 → 空列表，且不是 403。"""
    s = list_scenario
    resp = client.get("/api/items", headers=s["outsider_headers"])
    assert resp.status_code == 200
    assert _ids(resp.json()) == []


def test_participant_me_只列参与的事项(client, list_scenario):
    """init 发起了 A/C，只在 B 里是参与人 → participant=me 只剩 B。"""
    s = list_scenario
    body = client.get("/api/items?participant=me",
                      headers=s["init_headers"]).json()
    assert _ids(body) == [s["b"].id]


def test_state_open_排除终态(client, list_scenario):
    """C 已 completed → state=open 后只剩 A/B。"""
    s = list_scenario
    body = client.get("/api/items?state=open",
                      headers=s["init_headers"]).json()
    assert set(_ids(body)) == {s["a"].id, s["b"].id}


def test_两个筛选可叠加(client, list_scenario):
    """participant=me&state=open → 参与 且 未终态。"""
    s = list_scenario
    body = client.get("/api/items?participant=me&state=open",
                      headers=s["init_headers"]).json()
    assert _ids(body) == [s["b"].id]


def test_两通道逐字段相同(client, db_session, settings, list_scenario):
    """D2 + D3：同一 methods 函数、同一契约，两侧返回逐字段相同。"""
    s = list_scenario
    rest_body = client.get("/api/items?participant=me&state=open",
                           headers=s["init_headers"]).json()
    mcp_body = methods.mcp_list_items(
        db_session, settings, user_id=s["init"].id,
        participant="me", state="open",
    )
    assert rest_body == mcp_body


@pytest.mark.parametrize("query", ["participant=someone", "state=all"])
def test_非法筛选值两通道都被拒(client, db_session, settings,
                                list_scenario, query):
    """白名单之外的值一律 422，且两侧错误形状一致（不开自由文本口）。"""
    s = list_scenario
    resp = client.get(f"/api/items?{query}", headers=s["init_headers"])
    assert resp.status_code == 422, resp.json()
    rest_error = resp.json()

    kwargs = dict(pair.split("=") for pair in query.split("&"))
    with pytest.raises(ApiError) as exc:
        methods.mcp_list_items(db_session, settings, user_id=s["init"].id,
                               **kwargs)
    mcp_error = error_payload(exc.value)

    assert rest_error["error_code"] == mcp_error["error_code"] \
        == "VALIDATION_FAILED"
    assert rest_error["message"] == mcp_error["message"]


def test_列表不回显正文(client, db_session, list_scenario):
    """列表只给「用来挑选哪一条」的字段，不夹带正文（契约钉死）。"""
    s = list_scenario
    body = client.get("/api/items", headers=s["init_headers"]).json()
    assert all(
        set(item) == {"matter_id", "title", "status", "item_version",
                      "irreversible", "overall_deadline"}
        for item in body["items"]
    )
    # background / goal / options 这类正文键不得出现
    assert "background" not in str(body) and "options" not in str(body)
    assert db_session.scalar(select(Matter).where(Matter.id == s["a"].id))

"""待办 JSON 接口测试（v21）。

这些接口是给浏览器用的（Cookie 会话通道，跟 ``/api/meetings/*`` 一样），
业务逻辑转调 MCP 层——所以这一组测试的重点是**HTTP 层的正确性**：
认证、权限、状态码、CSRF 豁免边界。

业务逻辑本身的测试在 ``tests/api/test_todo_mcp.py``，不重复。
"""

import pytest

from hub.api import matters as matter_svc
from hub.db.models import Todo
from tests.conftest import make_user


@pytest.fixture()
def alice(db_session):
    return make_user(db_session, "alice", "pw-123456")


@pytest.fixture()
def bob(db_session):
    return make_user(db_session, "bob", "pw-123456")


@pytest.fixture()
def carol(db_session):
    return make_user(db_session, "carol", "pw-123456")


@pytest.fixture()
def board(db_session, alice, bob, carol):
    m = matter_svc.create_matter(
        db_session, initiator=alice, title="上线方案",
        goal="尽快上线", background="讨论方案",
        participant_ids=[bob.id, carol.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=5, draft_questions=["怎么看？"],
    )
    db_session.flush()
    return m


def _login(client, username):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": "pw-123456"})
    assert resp.status_code == 200


def _ai_todo(db_session, *, board, title, assignee_id=None):
    """造一条「AI 抽的、待确认」待办。"""
    t = Todo(matter_id=board.id, title=title, status="open",
             assignee_id=assignee_id, source="extracted_from_message",
             needs_confirm=True)
    db_session.add(t)
    db_session.flush()
    return t


# ------------------------------------------------------------------ 建待办


def test_建待办_成功(client, db_session, board, alice, bob):
    _login(client, "alice")

    resp = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "写上线方案", "assignee_id": bob.id})

    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "写上线方案"
    assert body["assignee"] == "bob"
    assert body["needs_confirm"] is False
    assert db_session.query(Todo).filter_by(title="写上线方案").count() == 1


def test_建待办_可暂不指派(client, board, alice):
    _login(client, "alice")

    resp = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "这个得有人跟"})

    assert resp.status_code == 201
    assert resp.json()["assignee"] is None


def test_建待办_多余字段返回422(client, board, alice):
    _login(client, "alice")

    resp = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "x", "status": "done"})

    assert resp.status_code == 422


def test_建待办_非成员被拒404(client, db_session, board, alice):
    outsider = make_user(db_session, "dave", "pw-123456")
    _login(client, "dave")

    resp = client.post(f"/api/matters/{board.id}/todos", json={"title": "偷偷建"})

    assert resp.status_code == 404


def test_建待办_未登录被拦(client, board):
    resp = client.post(f"/api/matters/{board.id}/todos", json={"title": "x"})

    assert "text/html" in resp.headers["content-type"]  # 303→登录页


# ------------------------------------------------------------------ 列表


def test_列待办_返回进度与参与人(client, db_session, board, alice, bob, carol):
    _login(client, "alice")
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "a", "assignee_id": bob.id})
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "b", "assignee_id": bob.id})

    body = client.get(f"/api/matters/{board.id}/todos").json()

    assert len(body["todos"]) == 2
    assert body["progress"]["total"] == 2
    assert body["progress"]["completion_rate"] == 0
    # 指派下拉需要参与人名单
    ids = {p["user_id"] for p in body["participants"]}
    assert {bob.id, carol.id} <= ids


def test_列待办_参与人名单供指派下拉(client, db_session, board, bob, carol):
    _login(client, "alice")

    body = client.get(f"/api/matters/{board.id}/todos").json()

    ids = {p["user_id"] for p in body["participants"]}
    assert bob.id in ids and carol.id in ids


def test_列待办_含AI待确认的计数(client, db_session, board, alice, bob):
    _login(client, "alice")
    client.post(f"/api/matters/{board.id}/todos", json={"title": "正式的"})
    _ai_todo(db_session, board=board, title="AI抽的")

    body = client.get(f"/api/matters/{board.id}/todos").json()

    assert len(body["todos"]) == 2          # 列表里有两条
    assert body["progress"]["total"] == 1  # 但只算一条正式的
    assert body["progress"]["awaiting_confirm"] == 1


def test_列待办_可按状态筛(client, board, alice, bob):
    _login(client, "alice")
    client.post(f"/api/matters/{board.id}/todos", json={"title": "待做"})
    done = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "做完了"})
    client.patch(f"/api/todos/{done.json()['todo_id']}", json={"status": "done"})

    body = client.get(f"/api/matters/{board.id}/todos?status=done").json()

    assert [t["title"] for t in body["todos"]] == ["做完了"]


def test_列待办_可只看我的(client, board, alice, bob, carol):
    _login(client, "bob")
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "bob的", "assignee_id": bob.id})
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "carol的", "assignee_id": carol.id})

    body = client.get(f"/api/matters/{board.id}/todos?assignee=me").json()

    assert [t["title"] for t in body["todos"]] == ["bob的"]


def test_列我的待办_跨事项(client, db_session, board, alice, bob, carol):
    _login(client, "bob")
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "甲板的活", "assignee_id": bob.id})
    other = matter_svc.create_matter(
        db_session, initiator=alice, title="乙板",
        goal="g", background="b", participant_ids=[bob.id, carol.id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=5,
        draft_questions=["q"],
    )
    db_session.flush()
    client.post(f"/api/matters/{other.id}/todos",
                json={"title": "乙板的活", "assignee_id": bob.id})

    body = client.get("/api/todos?status=all").json()

    titles = {t["title"] for t in body["todos"]}
    assert titles == {"甲板的活", "乙板的活"}
    # 跨事项查询必须带事项名，agent 和人都靠它分辨
    assert all(t["matter_title"] for t in body["todos"])


# ------------------------------------------------------------------ 改待办


def test_改待办_标完成(client, board, alice, bob):
    _login(client, "alice")
    todo = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "x", "assignee_id": bob.id}).json()

    resp = client.patch(f"/api/todos/{todo['todo_id']}", json={"status": "done"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_改待办_非法转移返回400(client, board, alice):
    _login(client, "alice")
    todo = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "x"}).json()
    client.patch(f"/api/todos/{todo['todo_id']}", json={"status": "dropped"})

    resp = client.patch(f"/api/todos/{todo['todo_id']}", json={"status": "done"})

    assert resp.status_code == 400


def test_改待办_不存在返回404(client, board, alice):
    _login(client, "alice")

    resp = client.patch("/api/todos/todo-不存在", json={"status": "done"})

    assert resp.status_code == 404


# ------------------------------------------------------------------ 确认 AI 待办


def test_确认AI待办_转正式(client, db_session, board, alice, bob):
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽的")

    resp = client.post(f"/api/todos/{t.id}/confirm", json={})

    assert resp.status_code == 200
    assert resp.json()["needs_confirm"] is False
    db_session.refresh(t)
    assert t.needs_confirm is False


def test_确认AI待办_可顺便指派(client, db_session, board, bob):
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽的没认出人")

    resp = client.post(f"/api/todos/{t.id}/confirm",
                       json={"assignee_id": bob.id})

    assert resp.status_code == 200
    assert resp.json()["assignee"] == "bob"


def test_确认AI待办_指派给外人被拒(client, db_session, board, alice):
    outsider = make_user(db_session, "dave", "pw-123456")
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽的")

    resp = client.post(f"/api/todos/{t.id}/confirm",
                       json={"assignee_id": outsider.id})

    assert resp.status_code == 400


def test_确认AI待办_重复确认幂等(client, db_session, board, alice):
    """人可能手快点两下，不该报错。"""
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽的")

    first = client.post(f"/api/todos/{t.id}/confirm", json={})
    second = client.post(f"/api/todos/{t.id}/confirm", json={})

    assert first.status_code == 200 and second.status_code == 200


def test_确认AI待办_非成员被拒(client, db_session, board, alice):
    make_user(db_session, "dave", "pw-123456")
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽的")
    _login(client, "dave")

    resp = client.post(f"/api/todos/{t.id}/confirm", json={})

    assert resp.status_code == 404


# ------------------------------------------------------------------ 删除（只给 AI 待办）


def test_删除AI待办_可以(client, db_session, board, alice):
    """AI 抽错了就该能拒掉——这正是 needs_confirm 闸门的意义。"""
    _login(client, "alice")
    t = _ai_todo(db_session, board=board, title="AI抽错的")

    resp = client.delete(f"/api/todos/{t.id}")

    assert resp.status_code == 200
    assert db_session.query(Todo).filter_by(id=t.id).count() == 0


def test_删除人工待办_被拒404改状态(client, board, alice):
    """人工建的是留痕，删了查不到「当初谁定的」。"""
    _login(client, "alice")
    todo = client.post(f"/api/matters/{board.id}/todos",
                       json={"title": "重要的"}).json()

    resp = client.delete(f"/api/todos/{todo['todo_id']}")

    assert resp.status_code == 400
    assert "改成" in resp.json()["message"]


# ------------------------------------------------------------------ 项目状态


def test_项目状态_与MCP同源(client, db_session, board, alice, bob):
    _login(client, "alice")
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "a", "assignee_id": bob.id})
    client.post(f"/api/matters/{board.id}/todos",
                json={"title": "b", "assignee_id": bob.id})

    body = client.get(f"/api/matters/{board.id}/project_status").json()

    assert body["progress"]["total"] == 2
    assert "共 2 项待办" in body["headline"]
    assert len(body["by_person"]) == 1


def test_项目状态_非成员被拒(client, db_session, board, alice):
    make_user(db_session, "dave", "pw-123456")
    _login(client, "dave")

    resp = client.get(f"/api/matters/{board.id}/project_status")

    assert resp.status_code == 404
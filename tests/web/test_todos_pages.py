"""待办网页界面测试（v21）。

界面是表单 POST + 整页刷新（与既有留言板一致）。这一组测的是**页面层**
的正确性：CSRF 防护、状态码、重定向、以及「填错不丢内容」这类体验细节。

渲染逻辑（完成率等口径）由 JSON 接口那组测试兜底，这里不重复。
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


def _login(client, username="alice"):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": "pw-123456"})
    assert resp.status_code == 200


def _ai_todo(db_session, *, board, title, assignee_id=None):
    t = Todo(matter_id=board.id, title=title, status="open",
             assignee_id=assignee_id, source="extracted_from_message",
             needs_confirm=True)
    db_session.add(t)
    db_session.flush()
    return t


# ------------------------------------------------------------------ 页面


def test_页面_正常渲染(client, board, alice):
    _login(client)

    resp = client.get(f"/matters/{board.id}/todos")

    assert resp.status_code == 200
    body = resp.text
    assert "待办事项" in body
    assert board.title in body
    assert "还没有待办" in body      # 空态提示


def test_页面_渲染待办与状态(client, db_session, board, alice, bob):
    _login(client)
    db_session.add(Todo(matter_id=board.id, title="正式的一条", status="open",
                        assignee_id=bob.id, source="manual"))
    db_session.commit()

    body = client.get(f"/matters/{board.id}/todos").text

    assert "正式的一条" in body
    assert "开始做" in body and "做完了" in body
    assert "人工建的不能删除" not in body  # 人工建的不该有删除按钮


def test_页面_AI待办显示确认按钮(client, db_session, board, alice):
    _login(client)
    _ai_todo(db_session, board=board, title="AI听出来的")

    body = client.get(f"/matters/{board.id}/todos").text

    assert "AI听出来的" in body
    assert "等你确认" in body
    assert "确认" in body and "不是这条" in body


def test_页面_未登录跳登录页(client, board):
    resp = client.get(f"/matters/{board.id}/todos", follow_redirects=False)

    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_页面_非成员看不到(client, db_session, board, alice):
    make_user(db_session, "dave", "pw-123456")
    _login(client, "dave")

    resp = client.get(f"/matters/{board.id}/todos", follow_redirects=False)

    # 404（不是 403，避免被拿去探测哪些事项存在）
    assert resp.status_code in (303, 404)


# ------------------------------------------------------------------ 建待办


def test_建待办_表单提交成功(client, db_session, board, alice, bob):
    _login(client)

    resp = client.post(f"/matters/{board.id}/todos",
                       data={"title": "整理报价明细", "detail": "含税",
                             "assignee_id": str(bob.id), "due_at": ""},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert f"/matters/{board.id}/todos" in resp.headers["location"]
    row = db_session.query(Todo).filter_by(title="整理报价明细").one()
    assert row.assignee_id == bob.id


def test_建待办_标题为空回页面且不丢内容(client, board, alice):
    """填错丢内容是最让人恼火的事，所以要带着填好的内容回页面。"""
    _login(client)

    resp = client.post(f"/matters/{board.id}/todos",
                       data={"title": "", "detail": "我写好的说明"})

    assert resp.status_code == 400
    assert "待办标题不能为空" in resp.text
    assert "我写好的说明" in resp.text   # 用户填的detail 还在页面上


def test_建待办_缺CSRF被拒(client, db_session, board, alice):
    """CSRF 防护：缺 token 必须挡住。

    必须绕开测试客户端的自动注入（``CsrfTestClient`` 会给已登录 POST 塞
    token），否则测的其实是「有没有带 token」，而不是「缺了会不会被挡」。
    """
    from fastapi.testclient import TestClient

    _login(client)
    db_session.query(Todo).delete()
    db_session.commit()

    bare = TestClient(client.app)          # 不会注入 CSRF
    bare.cookies.update(client.cookies)
    resp = bare.post(f"/matters/{board.id}/todos", data={"title": "没带csrf的"})

    assert resp.status_code == 403
    assert db_session.query(Todo).filter_by(title="没带csrf的").count() == 0


def test_建待办_指派非参与人被拒且不落库(client, db_session, board, alice):
    outsider = make_user(db_session, "dave", "pw-123456")
    _login(client)

    resp = client.post(f"/matters/{board.id}/todos",
                       data={"title": "派给外人", "assignee_id": str(outsider.id)})

    assert resp.status_code == 400
    assert db_session.query(Todo).filter_by(title="派给外人").count() == 0


# ------------------------------------------------------------------ 改状态


def test_改状态_标完成(client, db_session, board, alice, bob):
    _login(client)
    t = Todo(matter_id=board.id, title="要做的", status="open",
             assignee_id=bob.id, source="manual")
    db_session.add(t)
    db_session.commit()

    resp = client.post(f"/todos/{t.id}/update", data={"status": "done"},
                       follow_redirects=False)

    assert resp.status_code == 303
    db_session.refresh(t)
    assert t.status == "done"


def test_改状态_非法转移带错误回页面(client, db_session, board, alice):
    """dropped → done 不该静默成功，错误要能看见。"""
    _login(client)
    t = Todo(matter_id=board.id, title="放弃的", status="dropped",
             source="manual")
    db_session.add(t)
    db_session.commit()

    resp = client.post(f"/todos/{t.id}/update", data={"status": "done"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    # 错误信息确实传到了页面上
    follow = client.get(resp.headers["location"])
    assert "非法" in follow.text or "不能再改" in follow.text


def test_改状态_缺CSRF被拒(client, db_session, board, alice):
    from fastapi.testclient import TestClient

    _login(client)
    t = Todo(matter_id=board.id, title="x", status="open", source="manual")
    db_session.add(t)
    db_session.commit()

    bare = TestClient(client.app)
    bare.cookies.update(client.cookies)
    resp = bare.post(f"/todos/{t.id}/update", data={"status": "done"})

    assert resp.status_code == 403
    db_session.refresh(t)
    assert t.status == "open"


# ------------------------------------------------------------------ 确认/拒掉


def test_确认AI待办_转正式(client, db_session, board, alice):
    _login(client)
    t = _ai_todo(db_session, board=board, title="AI抽的")

    resp = client.post(f"/todos/{t.id}/confirm", data={"assignee_id": ""},
                       follow_redirects=False)

    assert resp.status_code == 303
    db_session.refresh(t)
    assert t.needs_confirm is False


def test_确认AI待办_可顺手指派(client, db_session, board, bob):
    _login(client)
    t = _ai_todo(db_session, board=board, title="AI没认出人")

    client.post(f"/todos/{t.id}/confirm", data={"assignee_id": str(bob.id)})

    db_session.refresh(t)
    assert t.needs_confirm is False
    assert t.assignee_id == bob.id


def test_拒掉AI待办_被删除(client, db_session, board, alice):
    _login(client)
    t = _ai_todo(db_session, board=board, title="AI抽错的")

    resp = client.post(f"/todos/{t.id}/reject", follow_redirects=False)

    assert resp.status_code == 303
    assert db_session.query(Todo).filter_by(id=t.id).count() == 0


def test_拒掉人工待办_被拦并给提示(client, db_session, board, alice):
    """人工建的是留痕，页面不该让用户点了才发现不行。"""
    _login(client)
    t = Todo(matter_id=board.id, title="重要的", status="open", source="manual")
    db_session.add(t)
    db_session.commit()

    resp = client.post(f"/todos/{t.id}/reject", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert db_session.query(Todo).filter_by(id=t.id).count() == 1


# ------------------------------------------------------------------ 入口链接


def test_留言板页有待办入口(client, board, alice):
    """从留言板要能找到待办页，否则功能等于没有。"""
    _login(client)

    body = client.get(f"/matters/{board.id}").text

    assert f"/matters/{board.id}/todos" in body
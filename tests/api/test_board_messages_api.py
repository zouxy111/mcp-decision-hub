"""留言板 JSON API（agent 侧出口）。

与 MCP 工具 post_message / list_messages / list_pending_questions 同一实现
（D3 单一事实源）：REST 只是第二个出口，形状由 hub.schemas.mcp_outputs 钉住。

2026-10-05 甲方要求（本文件覆盖）：
* 上传前必须本人同意 —— human_approved 必填，False/缺失一律 422
* 可以传 md 文件 —— attachment_name + attachment_md，正文存库、可读回
* 云端提问 → 本地处理 → 传回回答 —— kind=question / list_pending_questions /
  reply_to_message_id 闭环
* 一块板最多 1000 条（2026-10-05 甲方要求）
"""

import pytest

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.tokens import issue_token
from hub.domain import board as board_svc
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"
MESSAGE_KEYS = {
    "message_id", "matter_id", "kind", "acting_as", "content", "created_at",
    "human_approved", "human_approved_at", "attachment_name", "attachment_md",
    "asked_to_user_id", "reply_to_message_id", "question_status",
    "user_id", "username", "display_name", "responsibility",
}


@pytest.fixture()
def board_api(client, db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    outsider = make_user(db_session, "outsider")
    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id],
    )
    db_session.commit()
    headers = {}
    for name, user in (("init", init), ("alice", alice), ("outsider", outsider)):
        _token, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()
    return {
        "matter": matter, "init": init, "alice": alice, "outsider": outsider,
        "init_headers": headers["init"], "alice_headers": headers["alice"],
        "outsider_headers": headers["outsider"],
    }


def _post(client, s, headers, **payload):
    payload.setdefault("human_approved", True)
    return client.post(f"/api/items/{s['matter'].id}/messages",
                       json=payload, headers=headers)


def test_post_then_list_roundtrip(client, board_api):
    s = board_api
    resp = _post(client, s, s["alice_headers"],
                 content="agent 的判断：先用影子模式跑两周",
                 acting_as="agent_on_behalf")
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert set(body) == MESSAGE_KEYS
    assert body["content"] == "agent 的判断：先用影子模式跑两周"
    assert body["acting_as"] == "agent_on_behalf"
    assert body["username"] == "alice"
    assert body["kind"] == "message"
    # 同意留痕：Agent 代发也带上了「本人已确认」
    assert body["human_approved"] is True
    assert body["human_approved_at"]

    resp = client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["init_headers"])
    assert resp.status_code == 200
    assert resp.headers[CHANNEL] == "rest"
    data = resp.json()
    assert [m["content"] for m in data["messages"]] == \
        ["agent 的判断：先用影子模式跑两周"]
    assert {p["username"] for p in data["participants"]} == {"init", "alice"}
    assert data["message_count"] == 1
    assert data["message_limit"] == 1000


def test_without_human_consent_is_rejected(client, board_api):
    """上传前必须本人同意：human_approved=false 直接 422，且不落库。"""
    s = board_api
    resp = _post(client, s, s["alice_headers"], content="我没问过本人",
                 human_approved=False)
    assert resp.status_code == 422
    assert "本人同意" in resp.json()["message"]

    # 缺字段也算没同意（PostMessageIn 不给默认值）
    resp = client.post(f"/api/items/{s['matter'].id}/messages",
                       json={"content": "缺字段"}, headers=s["alice_headers"])
    assert resp.status_code == 422

    data = client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["init_headers"]).json()
    assert data["messages"] == []


def test_md_attachment_roundtrip(client, board_api):
    """内容长就传 md：全文存库、读回来还是原文。"""
    s = board_api
    md = "# 分析\n\n" + "\n".join(f"- 第 {i} 条依据" for i in range(1, 21))
    resp = _post(client, s, s["alice_headers"], content="详见附件",
                 attachment_name="分析.md", attachment_md=md)
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["attachment_name"] == "分析.md"
    assert body["attachment_md"] == md

    data = client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["init_headers"]).json()
    assert data["messages"][0]["attachment_md"] == md


def test_attachment_must_be_markdown(client, board_api):
    s = board_api
    resp = _post(client, s, s["alice_headers"], content="x",
                 attachment_name="报表.xlsx", attachment_md="二进制假装成文本")
    assert resp.status_code == 422
    assert "md" in resp.json()["message"]


def test_question_then_answer_loop(client, board_api):
    """云端提问 → 本地拉取 → 传回回答（提问自动置为已回答）。"""
    s = board_api
    resp = _post(client, s, s["init_headers"],
                 content="你负责的那块，风险你怎么看？",
                 kind="question", ask_user_id=s["alice"].id)
    assert resp.status_code == 201, resp.json()
    question_id = resp.json()["message_id"]
    assert resp.json()["question_status"] == "open"

    # alice 的 agent 在本地拉取待回答问题
    resp = client.get("/api/questions", headers=s["alice_headers"])
    assert resp.status_code == 200
    pending = resp.json()["questions"]
    assert [q["message_id"] for q in pending] == [question_id]
    assert pending[0]["matter_title"] == "T"

    # 发起人自己不该看到「待我回答」
    assert client.get("/api/questions",
                      headers=s["init_headers"]).json()["questions"] == []

    # 传回回答
    resp = _post(client, s, s["alice_headers"],
                 content="我的判断：风险可控，依据是测试数据。",
                 reply_to_message_id=question_id,
                 acting_as="agent_on_behalf")
    assert resp.status_code == 201, resp.json()
    assert resp.json()["kind"] == "answer"

    # 提问已被标记 answered，待回答列表清空
    data = client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["init_headers"]).json()
    by_id = {m["message_id"]: m for m in data["messages"]}
    assert by_id[question_id]["question_status"] == "answered"
    assert client.get("/api/questions",
                      headers=s["alice_headers"]).json()["questions"] == []


def test_question_requires_target(client, board_api):
    s = board_api
    resp = _post(client, s, s["init_headers"], content="问谁呢？",
                 kind="question")
    assert resp.status_code == 422
    assert "ask_user_id" in resp.json()["message"]

    # 不能问板外的人
    resp = _post(client, s, s["init_headers"], content="问外人",
                 kind="question", ask_user_id=s["outsider"].id)
    assert resp.status_code == 422


def test_board_capacity_is_1000(client, board_api, monkeypatch):
    """一块板最多 1000 条（测试里把上限压小，免得真发 1000 条）。"""
    s = board_api
    monkeypatch.setattr(board_svc, "MAX_MESSAGES_PER_BOARD", 2)
    assert _post(client, s, s["init_headers"], content="第一条").status_code == 201
    assert _post(client, s, s["alice_headers"], content="第二条").status_code == 201
    resp = _post(client, s, s["init_headers"], content="第三条")
    assert resp.status_code == 422
    assert "满" in resp.json()["message"]


def test_list_is_oldest_first(client, board_api):
    s = board_api
    for text in ("第一条", "第二条", "第三条"):
        assert _post(client, s, s["init_headers"], content=text).status_code == 201
    data = client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["init_headers"]).json()
    assert [m["content"] for m in data["messages"]] == \
        ["第一条", "第二条", "第三条"]


def test_non_member_gets_404_on_both_verbs(client, board_api):
    s = board_api
    assert client.get(f"/api/items/{s['matter'].id}/messages",
                      headers=s["outsider_headers"]).status_code == 404
    assert _post(client, s, s["outsider_headers"],
                 content="打扰一下").status_code == 404


def test_decision_only_for_initiator(client, board_api):
    s = board_api
    assert _post(client, s, s["alice_headers"], content="结论",
                 kind="decision").status_code == 422

    resp = _post(client, s, s["init_headers"],
                 content="结论：先试点知识守门", kind="decision")
    assert resp.status_code == 201
    assert resp.json()["kind"] == "decision"


def test_empty_content_rejected(client, board_api):
    s = board_api
    resp = _post(client, s, s["init_headers"], content="")
    assert resp.status_code == 422


def test_unknown_kind_rejected(client, board_api):
    s = board_api
    resp = _post(client, s, s["init_headers"], content="x", kind="shout")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 留言板是唯一流程：agent 侧「建板 → 发帖 → 读回」闭环 + 注册入口的自我介绍必填
# ---------------------------------------------------------------------------


def test_agent_declares_board_then_posts(client, db_session):
    """declare_item 建出来的就是留言板：建完能直接发帖、能读回。"""
    from hub.db.models import Matter

    init = make_user(db_session, "init")
    _token, plain = issue_token(db_session, user=init, name="init")
    db_session.commit()
    auth = {"Authorization": f"Bearer {plain}"}

    resp = client.post("/api/items",
                       json={"title": "agent 开的板子", "question": "要不要做？"},
                       headers=auth)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "open"
    matter = db_session.get(Matter, body["matter_id"])
    assert matter.mode == "board"

    resp = client.post(f"/api/items/{body['matter_id']}/messages",
                       json={"content": "我的判断：先做小范围试点。",
                             "human_approved": True},
                       headers=auth)
    assert resp.status_code == 201

    data = client.get(f"/api/items/{body['matter_id']}/messages",
                      headers=auth).json()
    assert [m["content"] for m in data["messages"]] == \
        ["我的判断：先做小范围试点。"]
    assert data["participants"][0]["username"] == "init"


def test_consume_endpoint_requires_self_intro(client, db_session):
    """JSON 注册口与 HTML 表单同一口径：姓名 / 负责内容都必填。"""
    from hub.db.models import Matter
    from hub.domain import invitation_links

    owner = make_user(db_session, "owner")
    matter = matter_svc.create_board_matter(
        db_session, initiator=owner, title="T", goal="G", background="B",
    )
    invitation = invitation_links.create_invitation_link(
        db=db_session, matter_id=matter.id, created_by=owner.id,
        expires_in_days=7, max_uses=5,
    )
    db_session.commit()
    assert isinstance(matter, Matter)

    resp = client.post(f"/consume/{invitation.short_code}",
                       json={"username": "newbie", "email": "n@example.com",
                             "password": "pw123456"})
    assert resp.status_code == 422

    resp = client.post(f"/consume/{invitation.short_code}",
                       json={"username": "newbie", "email": "n@example.com",
                             "password": "pw123456",
                             "display_name": "小新",
                             "responsibility": "负责跑验收"})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["success"] is True


def test_validate_invitation_returns_agent_brief(client, db_session):
    """邀请校验接口要把「怎么注册 / 怎么接入 / 怎么上传」交给对方 agent。"""
    from hub.domain import invitation_links

    owner = make_user(db_session, "owner")
    matter = matter_svc.create_board_matter(
        db_session, initiator=owner, title="给 agent 看的板子", goal="G",
        background="B",
    )
    invitation = invitation_links.create_invitation_link(
        db=db_session, matter_id=matter.id, created_by=owner.id,
        expires_in_days=7, max_uses=5,
    )
    db_session.commit()

    resp = client.get(f"/validate/{invitation.short_code}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    brief = body["agent_brief"]
    assert brief and len(brief) > 500
    # 关键内容必须都在：怎么接、怎么注册、怎么读（先读总结省上下文）、
    # 上传前必须本人同意、只有本人明确说要上传才上传、长内容走 md。
    for keyword in ("Streamable HTTP", "/mcp/", "hdt_", "姓名",
                    "同意", "上传", "markdown", "get_board_summary"):
        assert keyword in brief, keyword
    assert "五个问题" in brief


# ---------------------------------------------------------------------------
# 容量与写入提速（2026-10-05 甲方要求：留言板 1000 条 / 参与人 20 人 / 写快一点）
# ---------------------------------------------------------------------------


def test_board_participant_cap_is_20(db_session):
    """一块板最多 20 人（含发起人）—— 第 21 个人被挡在门外。"""
    init = make_user(db_session, "init")
    others = [make_user(db_session, f"p{i}") for i in range(20)]
    db_session.commit()

    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="T", goal="G",
        participant_ids=[u.id for u in others[:19]],
    )
    db_session.commit()
    assert matter.id

    with pytest.raises(ApiError) as exc:
        matter_svc.create_board_matter(
            db_session, initiator=init, title="T2", goal="G",
            participant_ids=[u.id for u in others],
        )
    assert exc.value.status_code == 422
    assert "20" in exc.value.message


def test_post_message_statement_budget(client, board_api, db_session):
    """写一条留言的 SQL 条数有上限（别退化成十几条查询 + 多次 commit）。"""
    from sqlalchemy import event

    s = board_api
    engine = db_session.get_bind()
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = _post(client, s, s["alice_headers"], content="一条普通留言")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 201
    # 认证 + 成员闸门 + 计数 + INSERT + 回读作者名片，留一点余量
    assert len(statements) <= 12, statements


def test_list_messages_has_no_n_plus_one(db_session, make_fake_llm):
    """读回 30 条留言的 SQL 条数不随留言条数增长（原来是每条 2 条查询）。"""
    from sqlalchemy import event

    init = make_user(db_session, "init")
    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="T", goal="G", participant_ids=[])
    for i in range(30):
        board_svc.post_message(db_session, matter_id=matter.id,
                               user_id=init.id, content=f"第 {i} 条")
    db_session.commit()

    engine = db_session.get_bind()
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        rows = board_svc.list_messages(db_session, matter_id=matter.id,
                                       user_id=init.id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(rows) == 30
    assert len(statements) <= 5, statements
    assert rows[0]["username"] == "init"

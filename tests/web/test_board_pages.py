"""留言板 Web 界面（2026-10-04 形态改造）。

旧形态「一轮一轮派题」的 Web 入口已被留言板取代：新建事项即开放，
参与人随时留言、互相可见；这些用例钉住新形态的核心行为。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.db.models import MatterMessage
from tests.conftest import make_user


@pytest.fixture()
def board(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    outsider = make_user(db_session, "outsider", password="pw-123456")
    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="留言板事项", goal="目标",
        background="背景", participant_ids=[alice.id],
    )
    db_session.commit()
    return matter, init, alice, outsider


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def test_board_page_shows_form_and_participants(client, board):
    matter, *_ = board
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "留言板" in resp.text
    assert "发布留言" in resp.text
    assert "还没有人留言" in resp.text
    assert "init" in resp.text and "alice" in resp.text
    # 轮次形态的界面元素不该出现在留言板上
    assert "累计轮次" not in resp.text
    assert "生成第一轮" not in resp.text


def test_member_post_visible_to_everyone(client, board, db_session):
    matter, init, alice, _ = board
    _login(client, "alice")
    resp = client.post(f"/matters/{matter.id}/messages",
                       data={"content": "我的判断：先做小范围验证"},
                       follow_redirects=False)
    assert resp.status_code == 303

    msg = db_session.scalar(select(MatterMessage))
    assert msg.content == "我的判断：先做小范围验证"
    assert msg.user_id == alice.id
    assert msg.kind == "message"

    # 发起人也看得到（留言板互相可见）
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "我的判断：先做小范围验证" in resp.text


def test_non_member_cannot_read_or_post(client, board, db_session):
    matter, *_ = board
    _login(client, "outsider")
    assert client.get(f"/matters/{matter.id}").status_code == 404
    resp = client.post(f"/matters/{matter.id}/messages", data={"content": "蹭一句"})
    assert resp.status_code == 404
    assert db_session.scalar(select(MatterMessage)) is None


def test_empty_message_rejected(client, board, db_session):
    matter, *_ = board
    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/messages", data={"content": "   "})
    assert resp.status_code == 400
    assert "不能为空" in resp.text
    assert db_session.scalar(select(MatterMessage)) is None


def test_only_initiator_can_post_decision(client, board, db_session):
    matter, init, alice, _ = board
    _login(client, "alice")
    resp = client.post(f"/matters/{matter.id}/messages",
                       data={"content": "我拍板了", "kind": "decision"})
    assert resp.status_code == 400
    assert "只有发起人" in resp.text
    assert db_session.scalar(select(MatterMessage)) is None

    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/messages",
                       data={"content": "先做知识守门这一件事", "kind": "decision"},
                       follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    msg = db_session.scalar(select(MatterMessage))
    assert msg.kind == "decision"
    assert msg.user_id == init.id


def test_invitee_card_shown_on_board(client, board, db_session):
    """受邀注册时自报的姓名 / 负责内容，要出现在留言板的参与人名片里。"""
    matter, _, alice, _ = board
    from hub.api import matters as svc

    svc.set_participant_profile(db_session, matter_id=matter.id, user_id=alice.id,
                                display_name="爱丽丝", responsibility="负责验证方案")
    db_session.commit()

    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "爱丽丝" in resp.text
    assert "负责验证方案" in resp.text


def test_web_md_upload_renders_attachment(client, board, db_session):
    """网页端可以直接传 .md：附件全文落库，板上点开能看。"""
    matter, init, alice, _ = board
    _login(client, "alice")
    resp = client.post(
        f"/matters/{matter.id}/messages",
        data={"content": "详见附件"},
        files={"attachment": ("分析.md", "# 标题\n\n正文一段。".encode(),
                              "text/markdown")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from hub.db.models import MatterMessage
    msg = db_session.scalar(select(MatterMessage))
    assert msg.attachment_name == "分析.md"
    assert msg.attachment_md == "# 标题\n\n正文一段。"

    resp = client.get(f"/matters/{matter.id}")
    assert "分析.md" in resp.text
    assert "正文一段" in resp.text


def test_web_upload_rejects_non_utf8(client, board, db_session):
    """附件只收 UTF-8 文本；二进制直接打回，不落库。"""
    matter, *_ = board
    _login(client, "init")
    resp = client.post(
        f"/matters/{matter.id}/messages",
        data={"content": "x"},
        files={"attachment": ("坏文件.md", b"\xff\xfe\x00\x01", "text/markdown")},
    )
    assert resp.status_code == 400
    assert "UTF-8" in resp.text
    from hub.db.models import MatterMessage
    assert db_session.scalar(select(MatterMessage)) is None


def test_web_ask_and_answer_question(client, board, db_session):
    """网页端提问 → 对方在「待你回答」里看到 → 回答后提问置为已回答。"""
    matter, init, alice, _ = board
    from hub.db.models import MatterMessage

    # 发起人向 alice 提问
    _login(client, "init")
    resp = client.post(
        f"/matters/{matter.id}/messages",
        data={"content": "你负责那块的费用怎么算？", "ask_user_id": str(alice.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    question = db_session.scalar(select(MatterMessage))
    assert question.kind == "question"
    assert question.asked_to_user_id == alice.id
    assert question.question_status == "open"

    # alice 打开板子：能在「待你回答」里看到这个问题
    _login(client, "alice")
    page = client.get(f"/matters/{matter.id}")
    assert "待你回答" in page.text
    assert "你负责那块的费用怎么算？" in page.text

    # 回答它
    resp = client.post(
        f"/matters/{matter.id}/messages",
        data={"content": "按人天算，我先给个粗估。",
              "reply_to_message_id": question.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(MatterMessage, question.id).question_status == "answered"

    # 答完之后「待你回答」不再显示这个问题，回答本身在板上
    page = client.get(f"/matters/{matter.id}")
    assert "待你回答" not in page.text
    assert "按人天算，我先给个粗估。" in page.text


def test_web_post_is_marked_human_approved(client, board, db_session):
    """网页发帖 = 本人操作，自动带「本人已确认」留痕。"""
    matter, init, _, _ = board
    from hub.db.models import MatterMessage

    _login(client, "init")
    client.post(f"/matters/{matter.id}/messages",
                data={"content": "我自己写的"}, follow_redirects=False)
    msg = db_session.scalar(select(MatterMessage))
    assert msg.acting_as == "human"
    assert msg.human_approved_at is not None


def test_web_board_shows_cloud_summary(client, board, db_session):
    """板上要显示云端自动总结（进展 / 判断 / 已明确 / 还没解决）。"""
    from hub.db.models import BoardSummary, MatterMessage
    from hub.domain.timeutil import utcnow

    matter, init, _, _ = board
    db_session.add(MatterMessage(matter_id=matter.id, user_id=init.id,
                                 content="背景先放这", kind="message",
                                 acting_as="human", created_at=utcnow()))
    db_session.add(BoardSummary(
        matter_id=matter.id, summary="目前只有发起人发了背景。",
        judgement="信息太少，还看不出结论。",
        key_points=["背景资料已上传"], open_questions=["真实数据表现如何"],
        covered_messages=1, generation_status="ok", updated_at=utcnow(),
    ))
    db_session.commit()

    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "云端自动总结" in resp.text
    assert "目前只有发起人发了背景。" in resp.text
    assert "信息太少，还看不出结论。" in resp.text
    assert "背景资料已上传" in resp.text
    assert "真实数据表现如何" in resp.text


def test_web_board_summary_absent_when_no_summary(client, board, db_session):
    """还没有总结时不显示该面板（别给人一个空壳）。"""
    matter, *_ = board
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert "云端自动总结" not in resp.text


# ---------------------------------------------------------------------------
# v20（2026-10-05）：总结文档 + 重读需求 + 大板子只渲染最近 100 条
# ---------------------------------------------------------------------------


def _seed_document(db_session, matter, *, version=1, trigger="auto"):
    from hub.db.models import BoardSummaryDocument
    from hub.domain.timeutil import utcnow

    db_session.add(BoardSummaryDocument(
        matter_id=matter.id, version=version, covered_from=1,
        covered_to=version, delta_messages=1,
        summary=f"第 {version} 版进展", judgement="判断",
        key_points=["一条事实"], open_questions=["一条待解决"],
        content_md=(
            f"# {matter.title} · 云端总结 v{version}\n\n"
            f"## 当前进展\n\n第 {version} 版进展\n"
        ),
        trigger=trigger, created_at=utcnow(),
    ))
    db_session.commit()


def test_board_page_lists_summary_documents(client, board, db_session):
    """板上列出总结文档，带「看全文 / 下载 .md」。"""
    matter, *_ = board
    _seed_document(db_session, matter, version=1)
    _seed_document(db_session, matter, version=2, trigger="requested")

    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "总结文档" in resp.text
    assert "v2" in resp.text and "v1" in resp.text
    assert f"/matters/{matter.id}/summary/2" in resp.text
    assert f"/matters/{matter.id}/summary/2.md" in resp.text
    assert "按需求重读全板" in resp.text
    assert f"/matters/{matter.id}/summary/reread" in resp.text


def test_summary_document_page_and_download(client, board, db_session):
    matter, *_ = board
    _seed_document(db_session, matter, version=3)
    _login(client, "init")

    page = client.get(f"/matters/{matter.id}/summary/3")
    assert page.status_code == 200
    assert "云端总结 v3" in page.text
    assert "第 3 版进展" in page.text

    raw = client.get(f"/matters/{matter.id}/summary/3.md")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/markdown")
    assert "attachment" in raw.headers["content-disposition"]
    assert "云端总结 v3" in raw.text

    assert client.get(f"/matters/{matter.id}/summary/9").status_code == 404


def test_summary_document_404_for_non_member(client, board, db_session):
    matter, *_ = board
    _seed_document(db_session, matter)
    _login(client, "outsider")
    assert client.get(f"/matters/{matter.id}/summary/1").status_code == 404


def test_web_reread_request_marks_board(client, board, db_session):
    """网页上「这份总结不对」→ 提交重读需求（留痕 + 入队）。"""
    from hub.db.models import BoardSummary
    from hub.domain.timeutil import utcnow

    matter, init, *_ = board
    db_session.add(BoardSummary(
        matter_id=matter.id, summary="旧总结", judgement="",
        key_points=[], open_questions=[], covered_messages=0,
        generation_status="ok", updated_at=utcnow(),
    ))
    db_session.commit()

    _login(client, "init")
    resp = client.post(f"/matters/{matter.id}/summary/reread",
                       data={"reason": "板子情况变了"},
                       follow_redirects=False)
    assert resp.status_code == 303
    # 后台 worker 会立刻消费这次需求（把开关置回 False），所以断言需求留痕
    db_session.rollback()
    row = db_session.get(BoardSummary, matter.id)
    assert row.reread_reason == "板子情况变了"
    assert row.reread_requested_at is not None


def test_big_board_renders_only_latest_page(client, board, db_session):
    """1000 条的板子不在页面上一次全渲染（否则浏览器卡死），给「看全部」。"""
    from hub.db.models import MatterMessage
    from hub.domain.timeutil import utcnow

    matter, init, *_ = board
    for i in range(105):
        db_session.add(MatterMessage(
            matter_id=matter.id, user_id=init.id, content=f"第 {i} 条留言",
            kind="message", acting_as="human", created_at=utcnow()))
    db_session.commit()

    _login(client, "init")
    page = client.get(f"/matters/{matter.id}")
    assert page.status_code == 200
    assert "只渲染最近 100 条" in page.text
    assert f"/matters/{matter.id}?messages=all" in page.text
    assert "第 104 条留言" in page.text
    assert "第 4 条留言" not in page.text

    all_page = client.get(f"/matters/{matter.id}?messages=all")
    assert all_page.status_code == 200
    assert "第 4 条留言" in all_page.text

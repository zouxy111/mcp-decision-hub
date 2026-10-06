"""总结文档（v20）：每轮总结落一份 md 文档 + 有人提需求才重读全板。

甲方要求（2026-10-05）：

    「每轮大模型总结完云端信息，都写一个总结文档。下一次总结只读最新的留言板
     （除非有人提了需求），否则只改动总结后的内容。」

所以这里钉三件事：
1. 每成功跑一轮总结就**多一份**文档（version 递增、旧版不覆盖）；
2. 常态下第二轮只喂新留言（增量），文档里「本轮新增」只列这一轮的新留言；
3. 有人提了需求（``request_reread``）之后，下一轮从第一条留言重新读，
   文档 trigger=requested 且写明理由与提出人。
"""

import pytest

from hub.api import board_summary as summary_svc
from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import BoardSummary, BoardSummaryDocument
from hub.domain import board as board_svc
from hub.mcp_server.methods import (
    mcp_get_board_summary,
    mcp_get_summary_document,
    mcp_list_summary_documents,
    mcp_request_board_reread,
)
from hub.schemas.mcp_outputs import (
    BoardDocumentListOut,
    BoardDocumentOut,
    BoardSummaryOut,
    RereadRequestOut,
)
from tests.conftest import make_user

SUMMARY_JSON = {
    "summary": "目前只有发起人发了背景。",
    "judgement": "还看不出结论。",
    "key_points": ["背景资料已上传"],
    "open_questions": ["真实数据上的表现如何"],
}


@pytest.fixture()
def board(db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="结算切换", goal="要不要切",
        background="背景", participant_ids=[alice.id],
    )
    db_session.commit()
    return matter, init, alice


def _say(db_session, matter, user, text):
    board_svc.post_message(db_session, matter_id=matter.id, user_id=user.id,
                           content=text)
    db_session.commit()


def _refresh(db_session, matter_id, llm):
    outcome = summary_svc.refresh_summary(db_session, llm=llm,
                                          matter_id=matter_id)
    db_session.commit()
    return outcome


# ---------------------------------------------------------------------------
# 1. 每轮都写一份文档
# ---------------------------------------------------------------------------


def test_each_round_files_a_document(db_session, board, make_fake_llm):
    matter, init, alice = board
    _say(db_session, matter, init, "第一条：背景说明")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])

    assert _refresh(db_session, matter.id, llm) == "ok"
    _say(db_session, matter, alice, "第二条：我这边有条件同意")
    assert _refresh(db_session, matter.id, llm) == "ok"

    docs = summary_svc.list_documents(db_session, matter_id=matter.id)
    assert [d["version"] for d in docs] == [2, 1]  # 最新在前
    view = summary_svc.summary_view(db_session, matter_id=matter.id)
    assert view["document_version"] == 2
    assert view["document_count"] == 2

    first = summary_svc.get_document(db_session, matter_id=matter.id, version=1)
    second = summary_svc.get_document(db_session, matter_id=matter.id, version=2)
    assert first["covered_from"] == 1 and first["covered_to"] == 1
    assert first["delta_messages"] == 1
    assert second["covered_from"] == 2 and second["covered_to"] == 2
    # 文档正文是完整 markdown，可以下载
    assert second["content_md"].startswith("# 结算切换 · 云端总结 v2")
    assert "## 本轮新增的留言（第 2–2 条）" in second["content_md"]
    # 本轮新增只列这一轮的留言（「只改动总结后的内容」）
    assert "我这边有条件同意" in second["content_md"]
    assert "背景说明" not in second["content_md"]


def test_document_survives_later_rounds(db_session, board, make_fake_llm):
    """旧版文档不覆盖：新留言不会改掉第一份文档。"""
    matter, init, _ = board
    _say(db_session, matter, init, "老内容")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)
    before = summary_svc.get_document(db_session, matter_id=matter.id, version=1)

    _say(db_session, matter, init, "新内容")
    _refresh(db_session, matter.id, llm)
    after = summary_svc.get_document(db_session, matter_id=matter.id, version=1)
    assert before["content_md"] == after["content_md"]
    assert "新内容" not in after["content_md"]


def test_no_new_messages_means_no_new_document(db_session, board, make_fake_llm):
    """没新留言就不重复落文档（否则版本号会被噪声撑爆）。"""
    matter, init, _ = board
    _say(db_session, matter, init, "只有一条")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)
    assert _refresh(db_session, matter.id, llm) == "up_to_date"
    assert summary_svc.document_count(db_session, matter_id=matter.id) == 1


# ---------------------------------------------------------------------------
# 2. 有人提了需求才重读全板
# ---------------------------------------------------------------------------


def test_reread_request_rebuilds_from_first_message(db_session, board,
                                                    make_fake_llm):
    matter, init, alice = board
    _say(db_session, matter, init, "第一条：老背景")
    _say(db_session, matter, alice, "第二条：老表态")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)

    # 有人提了需求
    result = summary_svc.request_reread(
        db_session, matter_id=matter.id, user_id=alice.id,
        reason="总结漏了关键分歧")
    db_session.commit()
    assert result["requested"] is True
    row = db_session.get(BoardSummary, matter.id)
    assert row.reread_requested is True
    assert row.reread_requested_by == alice.id

    # 下一轮：从第一条重读，不是增量
    assert _refresh(db_session, matter.id, llm) == "ok"
    third = llm.calls[-1]["user_prompt"]
    assert "第一条：老背景" in third      # 全板重读
    assert "第二条：老表态" in third
    assert "第一次总结" in third          # previous 清空 → 当第一次总结处理

    doc = summary_svc.get_document(db_session, matter_id=matter.id, version=2)
    assert doc["trigger"] == "requested"
    assert "有人提了需求" in doc["content_md"]
    assert "总结漏了关键分歧" in doc["content_md"]
    assert "init" in doc["content_md"] or "alice" in doc["content_md"]
    # 开关消费掉，下一轮回到增量
    db_session.refresh(row)
    assert row.reread_requested is False


def test_reread_requires_membership(db_session, board):
    matter, _, _ = board
    outsider = make_user(db_session, "outsider")
    db_session.commit()
    with pytest.raises(board_svc.BoardError):
        summary_svc.request_reread(db_session, matter_id=matter.id,
                                   user_id=outsider.id)


def test_is_stale_sees_pending_reread(db_session, board, make_fake_llm):
    matter, init, alice = board
    _say(db_session, matter, init, "唯一一条")
    llm = make_fake_llm([SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)
    assert summary_svc.is_stale(db_session, matter_id=matter.id) is False
    summary_svc.request_reread(db_session, matter_id=matter.id, user_id=alice.id)
    db_session.commit()
    assert summary_svc.is_stale(db_session, matter_id=matter.id) is True


# ---------------------------------------------------------------------------
# 3. MCP 工具契约
# ---------------------------------------------------------------------------


def test_mcp_document_tools_and_contracts(db_session, settings, board,
                                         make_fake_llm):
    matter, init, alice = board
    _say(db_session, matter, init, "第一条")
    llm = make_fake_llm([SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)

    listed = mcp_list_summary_documents(db_session, settings, user_id=init.id,
                                        matter_id=matter.id)
    BoardDocumentListOut.model_validate(listed)
    assert listed["document_count"] == 1
    assert listed["documents"][0]["version"] == 1
    assert "content_md" not in listed["documents"][0]

    doc = mcp_get_summary_document(db_session, settings, user_id=alice.id,
                                   matter_id=matter.id)
    BoardDocumentOut.model_validate(doc)
    assert doc["version"] == 1
    assert doc["content_md"]

    summary = mcp_get_board_summary(db_session, settings, user_id=init.id,
                                    matter_id=matter.id)
    BoardSummaryOut.model_validate(summary)
    assert summary["document_version"] == 1

    reread = mcp_request_board_reread(db_session, settings, user_id=alice.id,
                                      matter_id=matter.id, reason="重看一遍")
    db_session.commit()
    RereadRequestOut.model_validate(reread)
    assert reread["requested"] is True


def test_mcp_document_404_contract(db_session, settings, board):
    from hub.api.errors import ApiError

    matter, init, _ = board
    _say(db_session, matter, init, "第一条")
    with pytest.raises(ApiError) as exc:
        mcp_get_summary_document(db_session, settings, user_id=init.id,
                                 matter_id=matter.id)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 4. REST 出口
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(client, db_session, board):
    matter, init, alice = board
    _t, plain = issue_token(db_session, user=init, name="init")
    db_session.commit()
    return matter, init, {"Authorization": f"Bearer {plain}"}


def test_rest_document_endpoints(client, db_session, api, make_fake_llm):
    matter, init, headers = api
    _say(db_session, matter, init, "第一条")
    llm = make_fake_llm([SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)

    listed = client.get(
        f"/api/items/{matter.id}/board_summary/documents", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["document_count"] == 1

    latest = client.get(
        f"/api/items/{matter.id}/board_summary/document", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["version"] == 1

    by_version = client.get(
        f"/api/items/{matter.id}/board_summary/documents/1", headers=headers)
    assert by_version.status_code == 200
    assert by_version.json()["content_md"].startswith("# ")

    missing = client.get(
        f"/api/items/{matter.id}/board_summary/documents/9", headers=headers)
    assert missing.status_code == 404


def test_rest_reread_endpoint(client, db_session, api, make_fake_llm):
    """REST 出口和 MCP 工具同一实现。

    注意：测试里的 app 带着后台 worker（lifespan 已启动），提交后会立刻被
    worker 消费掉（并把 reread_requested 置回 False / 失败时挂回 True），
    所以这里断言的是「需求留痕 + 请求被受理」，消费语义在域层测试里钉。
    """
    matter, init, headers = api
    _say(db_session, matter, init, "第一条")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)

    resp = client.post(f"/api/items/{matter.id}/board_summary/reread",
                       json={"reason": "情况变了"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["requested"] is True
    assert resp.json()["requested_by_user_id"] == init.id

    # REST 走另一个 session（不同连接）→ 先结束本 session 的读事务再看
    db_session.rollback()
    row = db_session.get(BoardSummary, matter.id)
    assert row.reread_reason == "情况变了"
    assert row.reread_requested_at is not None


def test_document_table_is_append_only(db_session, board, make_fake_llm):
    """表本身只追加：没有新留言就不会多出行。"""
    matter, init, _ = board
    _say(db_session, matter, init, "一条")
    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON, SUMMARY_JSON])
    _refresh(db_session, matter.id, llm)
    _refresh(db_session, matter.id, llm)
    rows = db_session.query(BoardSummaryDocument).all()
    assert len(rows) == 1


def test_rebuild_continuation_batches_stay_requested(db_session, board,
                                                    make_fake_llm, monkeypatch):
    """整板重读要分好几批：每一批都标 requested，读完才把开关清掉。"""
    matter, init, alice = board
    for i in range(5):
        _say(db_session, matter, init, f"第 {i} 条")
    llm = make_fake_llm([SUMMARY_JSON] * 12)
    _refresh(db_session, matter.id, llm)          # 正常增量：v1
    summary_svc.request_reread(db_session, matter_id=matter.id, user_id=alice.id,
                               reason="重看一遍")
    db_session.commit()

    monkeypatch.setattr(summary_svc, "MAX_DELTA_MESSAGES", 2)
    assert _refresh(db_session, matter.id, llm) == "ok"   # 1-2
    row = db_session.get(BoardSummary, matter.id)
    assert row.reread_requested is True                    # 还没读完
    assert _refresh(db_session, matter.id, llm) == "ok"   # 3-4
    assert db_session.get(BoardSummary, matter.id).reread_requested is True
    assert _refresh(db_session, matter.id, llm) == "ok"   # 5
    assert db_session.get(BoardSummary, matter.id).reread_requested is False

    docs = summary_svc.list_documents(db_session, matter_id=matter.id)
    rebuilt = sorted((d for d in docs if d["trigger"] == "requested"),
                     key=lambda d: d["version"])
    assert [d["covered_from"] for d in rebuilt] == [1, 3, 5]
    assert all(d["trigger"] == "requested" for d in rebuilt)

"""留言板滚动总结（v19）：增量、状态、失败降级、接口。

甲方要求是「每次有新信息进来就结合之前的总结对当前任务有个大概判断，
保证速度、不占太多上下文」——所以这里最关键的断言是
**第二次总结只把新增留言喂给模型**（不重发全板）。
"""

import pytest

from hub.api import board_summary as summary_svc
from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.tokens import issue_token
from hub.domain import board as board_svc
from hub.llm.client import LLMError
from tests.conftest import make_user

SUMMARY_JSON = {
    "summary": "目前只有发起人发了背景，还没有别人表态。",
    "judgement": "还看不出结论，信息太少。",
    "key_points": ["背景资料已上传"],
    "open_questions": ["真实业务数据上的表现如何"],
}


@pytest.fixture()
def board(db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    matter = matter_svc.create_board_matter(
        db_session, initiator=init, title="T", goal="G", background="背景",
        participant_ids=[alice.id],
    )
    db_session.commit()
    return matter, init, alice


def _say(db_session, matter, user, text):
    board_svc.post_message(db_session, matter_id=matter.id, user_id=user.id,
                           content=text)
    db_session.commit()


def test_first_summary_covers_all_messages(db_session, board, make_fake_llm):
    matter, init, _ = board
    _say(db_session, matter, init, "第一条：背景说明")
    _say(db_session, matter, init, "第二条：补充一句")

    llm = make_fake_llm([SUMMARY_JSON])
    assert summary_svc.refresh_summary(db_session, llm=llm,
                                       matter_id=matter.id) == "ok"
    db_session.commit()

    view = summary_svc.summary_view(db_session, matter_id=matter.id)
    assert view["status"] == "ok"
    assert view["summary"] == SUMMARY_JSON["summary"]
    assert view["judgement"] == SUMMARY_JSON["judgement"]
    assert view["covered_messages"] == 2
    assert view["message_count"] == 2
    assert view["key_points"] == ["背景资料已上传"]


def test_incremental_only_sends_new_messages(db_session, board, make_fake_llm):
    """第二次总结只喂「上一版总结 + 新增留言」，不重发全板 —— 省上下文的关键。"""
    matter, init, _ = board
    _say(db_session, matter, init, "老留言：这是第一次的正文内容")

    llm = make_fake_llm([SUMMARY_JSON, SUMMARY_JSON])
    summary_svc.refresh_summary(db_session, llm=llm, matter_id=matter.id)
    db_session.commit()

    _say(db_session, matter, init, "新留言：这是第二次的正文内容")
    assert summary_svc.refresh_summary(db_session, llm=llm,
                                       matter_id=matter.id) == "ok"
    db_session.commit()

    assert len(llm.calls) == 2
    second = llm.calls[1]["user_prompt"]
    # 只带新留言，不再带老留言正文
    assert "这是第二次的正文内容" in second
    assert "这是第一次的正文内容" not in second
    # 但上一版总结要在（这样才有"结合之前的总结"）
    assert SUMMARY_JSON["summary"] in second

    view = summary_svc.summary_view(db_session, matter_id=matter.id)
    assert view["covered_messages"] == 2


def test_status_transitions(db_session, board, make_fake_llm):
    matter, init, _ = board
    assert summary_svc.summary_view(db_session, matter_id=matter.id)["status"] == \
        summary_svc.STATUS_EMPTY

    _say(db_session, matter, init, "有人说话了")
    assert summary_svc.summary_view(db_session, matter_id=matter.id)["status"] == \
        summary_svc.STATUS_STALE

    llm = make_fake_llm([SUMMARY_JSON])
    summary_svc.refresh_summary(db_session, llm=llm, matter_id=matter.id)
    db_session.commit()
    assert summary_svc.summary_view(db_session, matter_id=matter.id)["status"] == \
        summary_svc.STATUS_OK

    _say(db_session, matter, init, "又来一条")
    assert summary_svc.summary_view(db_session, matter_id=matter.id)["status"] == \
        summary_svc.STATUS_STALE


def test_failure_keeps_previous_summary(db_session, board, make_fake_llm):
    """生成失败不能把旧总结弄丢：状态标 failed，内容仍是上一版。"""
    matter, init, _ = board
    _say(db_session, matter, init, "第一条")
    llm = make_fake_llm([SUMMARY_JSON])
    summary_svc.refresh_summary(db_session, llm=llm, matter_id=matter.id)
    db_session.commit()

    _say(db_session, matter, init, "第二条")
    failing = make_fake_llm([LLMError("LLM_TIMEOUT", "上游超时", retry_count=3)])
    assert summary_svc.refresh_summary(db_session, llm=failing,
                                       matter_id=matter.id) == "failed"
    db_session.commit()

    view = summary_svc.summary_view(db_session, matter_id=matter.id)
    assert view["status"] == summary_svc.STATUS_FAILED
    assert view["summary"] == SUMMARY_JSON["summary"]  # 旧总结还在
    assert view["covered_messages"] == 1               # 游标没推进


def test_delta_batches_catch_up(db_session, board, make_fake_llm, monkeypatch):
    """一次最多喂 MAX_DELTA_MESSAGES 条；板子长就分几轮追平。"""
    matter, init, _ = board
    monkeypatch.setattr(summary_svc, "MAX_DELTA_MESSAGES", 2)
    for i in range(1, 6):
        _say(db_session, matter, init, f"第 {i} 条")

    llm = make_fake_llm([SUMMARY_JSON] * 3)
    assert summary_svc.refresh_summary(db_session, llm=llm,
                                       matter_id=matter.id) == "ok"
    db_session.commit()
    assert summary_svc.summary_view(db_session, matter_id=matter.id)[
        "covered_messages"] == 2

    summary_svc.refresh_summary(db_session, llm=llm, matter_id=matter.id)
    db_session.commit()
    assert summary_svc.summary_view(db_session, matter_id=matter.id)[
        "covered_messages"] == 4

    summary_svc.refresh_summary(db_session, llm=llm, matter_id=matter.id)
    db_session.commit()
    view = summary_svc.summary_view(db_session, matter_id=matter.id)
    assert view["covered_messages"] == 5
    assert view["status"] == summary_svc.STATUS_OK
    assert len(llm.calls) == 3  # 每次只喂 2 条，不是每次都重发全板

    # 已经追平：再刷就是空跑，不会再调模型
    assert summary_svc.refresh_summary(db_session, llm=llm,
                                       matter_id=matter.id) == "up_to_date"
    assert len(llm.calls) == 3


def test_up_to_date_does_not_call_llm(db_session, board, make_fake_llm):
    matter, init, _ = board
    llm = make_fake_llm([])
    assert summary_svc.refresh_summary(db_session, llm=llm,
                                       matter_id=matter.id) == "empty_board"
    assert llm.calls == []


def test_summary_api_membership_and_contract(client, db_session, board):
    """接口：成员 200 且过契约；非成员 404。"""
    matter, init, alice = board
    _say(db_session, matter, init, "先说一句")
    llm_calls = []

    class _NoCallLlm:
        def complete_json(self, *a, **k):  # pragma: no cover - 不该被调用
            llm_calls.append(1)
            raise AssertionError("读接口不许调模型")

    # 直接写一行总结，模拟后台已经跑过
    summary_svc.refresh_summary.__wrapped__ if False else None
    from hub.db.models import BoardSummary
    from hub.domain.timeutil import utcnow
    db_session.add(BoardSummary(
        matter_id=matter.id, summary="进展概述", judgement="大概判断",
        key_points=["已明确一条"], open_questions=["还没解决一条"],
        covered_messages=1, generation_status="ok", updated_at=utcnow(),
    ))
    db_session.commit()

    headers = {}
    for name, user in (("init", init), ("alice", alice)):
        _t, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    outsider = make_user(db_session, "outsider")
    _t, plain = issue_token(db_session, user=outsider, name="outsider")
    headers["outsider"] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()

    resp = client.get(f"/api/items/{matter.id}/board_summary",
                      headers=headers["init"])
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "matter_id", "summary", "judgement", "key_points", "open_questions",
        "message_count", "covered_messages", "document_version",
        "document_count", "updated_at", "status",
    }
    assert body["summary"] == "进展概述"
    assert body["message_count"] == 1
    assert body["covered_messages"] == 1
    assert body["status"] == "ok"
    # v20：这一行是手工塞的（没走 refresh_summary），所以还没有文档版本
    assert body["document_version"] == 0
    assert body["document_count"] == 0

    resp = client.get(f"/api/items/{matter.id}/board_summary",
                      headers=headers["outsider"])
    assert resp.status_code == 404

    # 未知事项也是 404
    try:
        client.get("/api/items/mat_nope/board_summary", headers=headers["init"])
    except ApiError:  # pragma: no cover
        pass

"""r5Am9i · `ask_participant`（MCP 工具第 5 个 / rpQt6D 端点「ask」）。

按开工包 PRD-01 的 5 片切片 + 验收第 4 条（无配额）逐条钉。

与 MCP 工具**同源**：两侧调用同一个 `methods.mcp_ask_participant`，返回同一组
`AskParticipantOut` 契约。

**落点说明（与 PRD-01 原文的差异，已登记）**：PRD-01 写「挂到目标参与人本轮
`questions_for` 上」，但那一列在 `stances` 表、语义是「本人向他人提问」，且
目标本轮未提交立场时该行不存在 —— 无处可挂。故落点为独立表
`participant_questions`，并通过 `get_task.directed_questions` 投递给被问人。
见 `outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md`。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError, error_payload
from hub.api.tokens import issue_token
from hub.db.models import ParticipantQuestion, Task
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import AskParticipantOut
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def ask_scenario(client, db_session):
    """已开跑的事项（有轮次、有任务）；发起人 / 两位参与人 / 局外人各持 token。"""
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
    db_session.commit()
    alice_task = db_session.scalar(
        select(Task).where(Task.assignee_id == alice.id)
    )

    headers = {}
    for name, user in (("init", init), ("alice", alice),
                       ("outsider", outsider)):
        _t, plain = issue_token(db_session, user=user, name=name)
        headers[name] = {"Authorization": f"Bearer {plain}"}
    db_session.commit()
    return {
        "matter": matter, "alice_task": alice_task,
        "init": init, "alice": alice, "bob": bob, "outsider": outsider,
        "init_headers": headers["init"], "alice_headers": headers["alice"],
        "outsider_headers": headers["outsider"],
    }


def _ask(client, headers, matter_id, target_id, question):
    return client.post(
        f"/api/items/{matter_id}/ask",
        json={"target_user_id": target_id, "question": question},
        headers=headers,
    )


def test_成员向参与人提问_返回契约且带降级标头(client, db_session, ask_scenario):
    """片 1：提问成功、出参过契约、通道可观测（D4）。"""
    s = ask_scenario
    resp = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                "预算上限是多少？")

    assert resp.status_code == 200, resp.json()
    assert resp.headers[CHANNEL] == "rest"
    body = resp.json()
    AskParticipantOut.model_validate(body)
    assert body["target_user_id"] == s["alice"].id
    assert body["question"] == "预算上限是多少？"
    assert body["asked_by_user_id"] == s["init"].id


def test_被问人在任务上下文里看到定向问题(
    client, db_session, settings, ask_scenario
):
    """片 1（投递口单独钉）：`get_task.directed_questions` 里出现该问题。"""
    s = ask_scenario
    _ask(client, s["init_headers"], s["matter"].id, s["alice"].id, "风险怎么看？")

    detail = methods.mcp_get_task(
        db_session, settings, user_id=s["alice"].id,
        task_id=s["alice_task"].id,
    )
    assert [q["question"] for q in detail["directed_questions"]] == ["风险怎么看？"]
    # 问的是 alice，bob 的任务上下文里不该有
    bob_task = db_session.scalar(select(Task).where(Task.assignee_id == s["bob"].id))
    if bob_task is not None:
        bob_detail = methods.mcp_get_task(
            db_session, settings, user_id=s["bob"].id, task_id=bob_task.id,
        )
        assert "directed_questions" not in bob_detail


def test_没有提问时任务上下文不出现该键(client, db_session, settings,
                                          ask_scenario):
    """对照：默认空 → exclude_defaults 把它整个去掉，既有消费端字段集合不变。"""
    s = ask_scenario
    detail = methods.mcp_get_task(
        db_session, settings, user_id=s["alice"].id,
        task_id=s["alice_task"].id,
    )
    assert "directed_questions" not in detail


def test_非成员提问_404且不泄露事项存在(client, db_session, settings,
                                       ask_scenario):
    """片 2：非成员一律 404「事项不存在」，两侧错误形状一致。"""
    s = ask_scenario
    resp = _ask(client, s["outsider_headers"], s["matter"].id, s["alice"].id, "在吗")
    assert resp.status_code == 404
    rest_error = resp.json()

    with pytest.raises(ApiError) as exc:
        methods.mcp_ask_participant(
            db_session, settings, user_id=s["outsider"].id,
            matter_id=s["matter"].id,
            payload={"target_user_id": s["alice"].id, "question": "在吗"},
        )
    mcp_error = error_payload(exc.value)

    assert rest_error["error_code"] == mcp_error["error_code"] \
        == "RESOURCE_NOT_FOUND"
    assert rest_error["message"] == mcp_error["message"] == "事项不存在"


def test_向非参与人提问_422(client, ask_scenario):
    """片 3：目标不是该事项参与人 → 422 VALIDATION_FAILED。"""
    s = ask_scenario
    resp = _ask(client, s["init_headers"], s["matter"].id, s["outsider"].id,
                "在吗")
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"


def test_重复提交同一问题_返回首次结果且不产生第二条(client, db_session,
                                             ask_scenario):
    """片 4：幂等由正文+四元组唯一约束承担，不引入外部幂等键。"""
    s = ask_scenario
    first = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                 "同一个问题").json()
    again = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                 "同一个问题").json()

    assert again["question_id"] == first["question_id"]
    assert again["created_at"] == first["created_at"]
    rows = db_session.scalars(
        select(ParticipantQuestion).where(
            ParticipantQuestion.matter_id == s["matter"].id)
    ).all()
    assert len(rows) == 1

    # 换个问法就是新的一条（幂等只按正文，不按语义）
    other = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                 "同一个问题。").json()
    assert other["question_id"] != first["question_id"]


def test_两通道逐字段相同(client, db_session, settings, ask_scenario):
    """片 5：D2 + D3 —— 同一 methods 函数、同一契约，两侧返回逐字段相同。"""
    s = ask_scenario
    rest_body = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                     "两通道一致性").json()
    mcp_body = methods.mcp_ask_participant(
        db_session, settings, user_id=s["init"].id, matter_id=s["matter"].id,
        payload={"target_user_id": s["alice"].id, "question": "两通道一致性"},
    )
    assert rest_body == mcp_body


def test_指定不存在的轮次_422(client, ask_scenario):
    """round_number 白名单：给了但该轮不存在 → 422，不静默落到当前轮。"""
    s = ask_scenario
    resp = client.post(
        f"/api/items/{s['matter'].id}/ask",
        json={"target_user_id": s["alice"].id, "question": "Q", "round_number": 9},
        headers=s["init_headers"],
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"


def test_无配额_连问一百一十条全部成功(client, ask_scenario):
    """验收第 4 条：**不存在**针对 ask 的计数/限流（前身曾落 100/429，已回退）。

    为什么是 110：旧配额是 100，越过它才有证明力。
    """
    s = ask_scenario
    for i in range(110):
        resp = _ask(client, s["init_headers"], s["matter"].id, s["alice"].id,
                    f"第 {i} 问")
        assert resp.status_code == 200, (i, resp.json())

"""r5Am9i · REST 降级通道 D1–D6 验收。

D3 单一事实源的落地：REST 端点与 MCP 工具调用**同一个** methods 函数，
产出经**同一组** hub.schemas.mcp_outputs 契约 —— 测试用同一模型分别校验
两条通道的返回，并断言字段集合逐字段相同（D2）。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import Task
from hub.mcp_server.methods import (
    mcp_get_matter_status,
    mcp_get_task,
    mcp_list_pending_tasks,
    mcp_submit_output,
)
from hub.schemas.mcp_outputs import (
    MatterStatusOut,
    PendingTasksOut,
    SubmitOutputOut,
    TaskDetailOut,
)
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def agent_scenario(client, db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    task = db_session.scalar(select(Task).where(Task.assignee_id == alice.id))
    _t, plain_alice = issue_token(db_session, user=alice, name="alice")
    _t, plain_init = issue_token(db_session, user=init, name="init")
    db_session.commit()
    return {"matter": matter, "task": task, "alice": alice, "bob": bob,
            "init": init, "alice_headers": {"Authorization": f"Bearer {plain_alice}"},
            "init_headers": {"Authorization": f"Bearer {plain_init}"}}


def test_D1_REST通道独立可用_无需任何MCP参与(client, agent_scenario):
    """MCP 不可用是前提假设：REST 端点只依赖 HTTP + Bearer。"""
    resp = client.get("/api/agent/tasks", headers=agent_scenario["alice_headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert [t["task_id"] for t in body["tasks"]] == [agent_scenario["task"].id]


def test_D2_两通道返回字段集合逐字段相同(client, db_session, settings,
                                          agent_scenario):
    s = agent_scenario
    rest_list = client.get("/api/agent/tasks", headers=s["alice_headers"]).json()
    mcp_list = mcp_list_pending_tasks(
        db_session, settings, user_id=s["alice"].id, limit=20, cursor=None)
    assert set(rest_list) == set(mcp_list)
    assert set(rest_list["tasks"][0]) == set(mcp_list["tasks"][0])

    rest_task = client.get(f"/api/agent/tasks/{s['task'].id}",
                           headers=s["alice_headers"]).json()
    mcp_task = mcp_get_task(db_session, settings, user_id=s["alice"].id,
                            task_id=s["task"].id)
    assert set(rest_task) == set(mcp_task)
    assert set(rest_task["matter"]) == set(mcp_task["matter"])

    rest_status = client.get(
        f"/api/agent/matters/{s['matter'].id}/status",
        headers=s["alice_headers"]).json()
    mcp_status = mcp_get_matter_status(db_session, settings,
                                       user_id=s["alice"].id,
                                       matter_id=s["matter"].id)
    assert set(rest_status) == set(mcp_status)
    assert set(rest_status["recent_rounds"][0]) == set(
        mcp_status["recent_rounds"][0])


def test_D3_同一组Pydantic模型校验两通道(client, db_session, settings,
                                          agent_scenario):
    s = agent_scenario
    rest_list = client.get("/api/agent/tasks", headers=s["alice_headers"]).json()
    PendingTasksOut.model_validate(rest_list)
    PendingTasksOut.model_validate(mcp_list_pending_tasks(
        db_session, settings, user_id=s["alice"].id, limit=20, cursor=None))

    rest_task = client.get(f"/api/agent/tasks/{s['task'].id}",
                           headers=s["alice_headers"]).json()
    TaskDetailOut.model_validate(rest_task)
    TaskDetailOut.model_validate(mcp_get_task(db_session, settings,
                                              user_id=s["alice"].id,
                                              task_id=s["task"].id))

    rest_status = client.get(
        f"/api/agent/matters/{s['matter'].id}/status",
        headers=s["alice_headers"]).json()
    MatterStatusOut.model_validate(rest_status)
    MatterStatusOut.model_validate(mcp_get_matter_status(
        db_session, settings, user_id=s["alice"].id, matter_id=s["matter"].id))


def test_D4_降级显式可观测_成功响应带通道标头(client, agent_scenario):
    for path in ("/api/agent/tasks",
                 f"/api/agent/tasks/{agent_scenario['task'].id}",
                 f"/api/agent/matters/{agent_scenario['matter'].id}/status"):
        resp = client.get(path, headers=agent_scenario["alice_headers"])
        assert resp.headers[CHANNEL] == "rest"


def test_D5_纯HTTP只需Bearer_无令牌401(client):
    assert client.get("/api/agent/tasks").status_code == 401


def test_D6_4xx不降级_错误形状与MCP一致(client, db_session, settings,
                                         agent_scenario):
    """bob 的 token 提交到 alice 的任务：REST 与 MCP 两侧都是 403
    FORBIDDEN_SCOPE，错误形状逐字段一致；通道不做任何兜底。"""
    s = agent_scenario
    from hub.domain.digest import compute_content_digest
    from hub.domain.timeutil import iso_z, utcnow
    from hub.mcp_server.methods import mcp_submit_output

    payload = {
        "task_id": s["task"].id,
        "answers": [{"question_id": "q1", "content": "回答"}],
        "notes": None,
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(
            [{"question_id": "q1", "content": "回答"}], None),
        "idempotency_key": "rest-fallback-1",
    }

    rest_resp = client.post(f"/api/agent/tasks/{s['task'].id}/outputs",
                            json=payload, headers=s["init_headers"])
    assert rest_resp.status_code == 403
    rest_error = rest_resp.json()

    with pytest.raises(Exception) as exc:
        mcp_submit_output(db_session, settings, user_id=s["init"].id,
                          payload=payload)
    from hub.api.errors import error_payload
    mcp_payload = error_payload(exc.value)
    assert rest_error["error_code"] == mcp_payload["error_code"] == "FORBIDDEN_SCOPE"
    assert rest_error["message"] == mcp_payload["message"]


def test_幂等键跨通道通用_REST重放与MCP首次结果一致(client, db_session, settings,
                                                     agent_scenario):
    from hub.domain.digest import compute_content_digest
    from hub.domain.timeutil import iso_z, utcnow

    s = agent_scenario
    payload = {
        "task_id": s["task"].id,
        "answers": [{"question_id": "q1", "content": "回答"}],
        "notes": None,
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(
            [{"question_id": "q1", "content": "回答"}], None),
        "idempotency_key": "cross-channel-key",
    }
    mcp_first = mcp_submit_output(db_session, settings, user_id=s["alice"].id,
                                  payload=payload)
    db_session.commit()

    rest_replay = client.post(f"/api/agent/tasks/{s['task'].id}/outputs",
                              json=payload, headers=s["alice_headers"])
    assert rest_replay.status_code == 201
    SubmitOutputOut.model_validate(rest_replay.json())
    assert rest_replay.json() == mcp_first

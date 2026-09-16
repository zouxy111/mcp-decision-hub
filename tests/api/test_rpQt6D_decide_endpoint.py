"""rpQt6D · 端点缺口之一：`POST /api/items/{id}/decide`。

与 MCP 工具 `decide_item` **同源**（`test_rest_fallback.py` D3 单一事实源）：
两侧调用同一个 `hub.mcp_server.methods.mcp_decide_item`，返回同一组
`hub.schemas.mcp_outputs.ResolutionView` 契约。

为什么这条必须先做：`hub/domain/resolution.py` 的决策枚举（approved /
modified / rejected）与 `resolutions.decide_resolution` 的守卫（仅发起人、
irreversible 拒绝、非 awaiting_decision 拒绝、version+status 乐观锁）都已
存在——缺的只是通道。所以本切片不新写业务逻辑，只做「把已有服务接到
HTTP/MCP 两个出口上」，并钉住两个出口不会各写一份。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError, error_payload
from hub.api.tokens import issue_token
from hub.db.models import Resolution, Round
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import ResolutionView
from tests.conftest import make_user

CHANNEL = "X-Hub-Channel"


@pytest.fixture()
def decide_scenario(client, db_session):
    """awaiting_decision 的事项 + pending_review 的 v1 决议；发起人与参与者各持 token。"""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    rnd = db_session.scalar(select(Round))
    db_session.add(
        Resolution(
            matter_id=matter.id, source_round_id=rnd.id, version=1,
            status="pending_review",
            recommendation="采用方案 A", rationale="依据",
            risks=["风险"], divergences=["分歧"], cited_rounds=[1],
        )
    )
    matter.status = "awaiting_decision"
    db_session.commit()

    _t, plain_init = issue_token(db_session, user=init, name="init")
    _t, plain_alice = issue_token(db_session, user=alice, name="alice")
    db_session.commit()
    return {
        "matter": matter, "init": init, "alice": alice,
        "init_headers": {"Authorization": f"Bearer {plain_init}"},
        "alice_headers": {"Authorization": f"Bearer {plain_alice}"},
    }


def test_发起人经REST拍板_返回决议契约且带降级标头(client, decide_scenario):
    """新端点的存在性 + 形状 + 通道可观测性。"""
    s = decide_scenario
    resp = client.post(
        f"/api/items/{s['matter'].id}/decide",
        json={"decision": "approved", "expected_version": 1},
        headers=s["init_headers"],
    )

    assert resp.status_code == 200, resp.json()
    assert resp.headers[CHANNEL] == "rest"  # D4 降级显式可观测

    body = resp.json()
    ResolutionView.model_validate(body)  # 过同一组契约
    assert body["status"] == "approved"
    assert body["version"] == 2  # 拍板后 version 递增
    assert body["decided_at"] is not None


def test_非发起人拍板被拒_且与MCP侧错误形状一致(
    client, db_session, settings, decide_scenario
):
    """D6：4xx 不做任何兜底；两侧错误形状逐字段相同 —— 证明同一份实现。"""
    s = decide_scenario
    resp = client.post(
        f"/api/items/{s['matter'].id}/decide",
        json={"decision": "approved", "expected_version": 1},
        headers=s["alice_headers"],
    )
    assert resp.status_code == 403
    rest_error = resp.json()

    with pytest.raises(ApiError) as exc:
        methods.mcp_decide_item(
            db_session, settings, user_id=s["alice"].id,
            matter_id=s["matter"].id,
            payload={"decision": "approved", "expected_version": 1},
        )
    mcp_error = error_payload(exc.value)

    assert rest_error["error_code"] == mcp_error["error_code"] == "FORBIDDEN_SCOPE"
    assert rest_error["message"] == mcp_error["message"]


def test_非法决策值被schema拦下且不落库(client, db_session, decide_scenario):
    """枚举在 schema 层拒绝（rpQt6D 硬约束：非法枚举必须 422 且不落库）。"""
    s = decide_scenario
    resp = client.post(
        f"/api/items/{s['matter'].id}/decide",
        json={"decision": "maybe", "expected_version": 1},
        headers=s["init_headers"],
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"

    db_session.expire_all()
    still = db_session.scalar(select(Resolution))
    assert still.status == "pending_review"
    assert still.version == 1

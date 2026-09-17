"""2026-09-17 owner 裁定落地（TDD）。

裁定 1（P0 修复）：REST/MCP 拍板成功后入 resume_queue，与网页同一完结链路
——回归柠檬果 2026-09-17 报的 P0（拍板 200 后滞留 awaiting_decision，
仅重启进程才被启动恢复逻辑归档，稳定复现 3/3）。
裁定 2：不可逆事项定性为缺口——只能发起人本人在网页完结；API/MCP 照旧
拒绝；建不可逆事项必填理由全通道生效；拒绝文案更正。
顺带：`mcp_declare_item` 的 `max_rounds` 遵循 settings（原硬编码 10）。

current_round（裁定 3）的断言在 tests/api/test_rpQt6D_digest_onepager.py。
网页端到端的不可逆拍板在 tests/web/test_decision_page.py。
"""

import asyncio
import time

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.resolutions import decide_resolution
from hub.api.tokens import issue_token
from hub.db.models import Matter, Resolution, Round, RoundSummary
from hub.mcp_server import methods
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    return {n: make_user(db_session, n) for n in ("init", "alice", "bob")}


def _awaiting_decision_matter(db_session, users, *, irreversible=False):
    """awaiting_decision 事项 + pending_review v1 决议（可勾选不可逆）。

    round 必须是 closed 且有 ok 摘要——真实链路里 awaiting_decision 必经
    「收齐 → 摘要 → 草案 → accept」，图的 propagate 路由
    （_compute_branch_route）也以此为先决；只造状态不造轮次史实会让
    resume 走不到 after_decision（调试实录：round=open 时 branch 判 done）。
    """
    kwargs = {}
    if irreversible:
        kwargs = {"irreversible": True,
                  "irreversible_reason": "涉及生产数据删除，不可逆"}
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B",
        participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"], **kwargs,
    )
    matter_svc.start_matter(db_session, matter_id=matter.id,
                            actor=users["init"])
    rnd = db_session.scalar(
        select(Round).where(Round.matter_id == matter.id))
    db_session.execute(
        update(Round).where(Round.id == rnd.id).values(status="closed"))
    db_session.add(
        RoundSummary(round_id=rnd.id, matter_id=matter.id,
                     consensus_points=["共识"], divergences=[],
                     blind_spots=[], open_questions=[],
                     convergence="converged", generation_status="ok")
    )
    db_session.add(
        Resolution(matter_id=matter.id, source_round_id=rnd.id, version=1,
                   status="pending_review", recommendation="采用方案 A",
                   rationale="依据", risks=["风险"], divergences=["分歧"],
                   cited_rounds=[1])
    )
    matter.status = "awaiting_decision"
    db_session.commit()
    return matter


# ---------- 裁定 1：拍板后入队（P0 回归） ----------


def test_REST拍板后事项推进终态_无需重启(client, session_factory, db_session,
                                       users):
    """柠檬果 P0 的原复现路径：REST 拍板 200 后轮询，必须直接 completed，
    不许依赖重启恢复。resume_worker 经 resume_queue 异步传播（轮询等待）。"""
    matter = _awaiting_decision_matter(db_session, users)
    _t, plain = issue_token(db_session, user=users["init"], name="init")
    db_session.commit()

    resp = client.post(
        f"/api/items/{matter.id}/decide",
        json={"decision": "approved", "expected_version": 1},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200, resp.json()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with session_factory() as s:
            if s.get(Matter, matter.id).status == "completed":
                break
        time.sleep(0.1)
    with session_factory() as s:
        assert s.get(Matter, matter.id).status == "completed"


def test_MCP工具decide_item成功后入resume_queue(db_session, session_factory,
                                                settings, users, monkeypatch):
    """裁定 1 的 MCP 半侧接线：register_tools 收到的 resume_queue 必须被
    decide_item 闭包在 _call 成功（即 commit）之后入队。

    反射调用工具 fn（绕过 HTTP/auth 层，monkeypatch 身份查找）；队列断言
    替代真实 worker——worker 行为由 test_REST拍板后事项推进终态_无需重启 与
    tests/web/test_decision_page.py 的网页路径共同覆盖。
    """
    from fastmcp import FastMCP

    from hub.mcp_server.tools import register_tools

    matter = _awaiting_decision_matter(db_session, users)
    queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr("hub.mcp_server.tools._current_user_id",
                        lambda: users["init"].id)
    mcp = FastMCP("test-registry")
    register_tools(mcp, session_factory, settings, resume_queue=queue)

    fn = mcp._tool_manager._tools["decide_item"].fn
    result = fn(matter_id=matter.id, decision="approved", expected_version=1)

    assert result["status"] == "approved"
    assert queue.get_nowait() == (matter.id, "decide")


# ---------- 裁定 2：不可逆事项 ----------


def test_不可逆事项_网页通道发起人本人可完结(db_session, users):
    """裁定 2：不可逆 ≠ 不能完结，而是只能发起人本人在网页完结。
    channel="web" 由网页路由传入（routes_decision.py）。"""
    matter = _awaiting_decision_matter(db_session, users, irreversible=True)

    res = decide_resolution(
        db_session, matter_id=matter.id, actor=users["init"],
        decision="approved", expected_version=1, channel="web",
    )

    assert res.status == "approved"
    assert res.version == 2


def test_不可逆事项_API与MCP通道仍拒绝_文案不再自相矛盾(db_session, settings,
                                                       users):
    """09-11 底线不破：代理/接口通道不得终裁不可逆事项（守卫原样保留）。
    文案更正：调用者就是发起人本人，旧文案「须发起人本人拍板」自相矛盾。"""
    matter = _awaiting_decision_matter(db_session, users, irreversible=True)

    with pytest.raises(ApiError) as exc:
        decide_resolution(
            db_session, matter_id=matter.id, actor=users["init"],
            decision="approved", expected_version=1,
        )
    assert exc.value.status_code == 403
    assert "须在网页由发起人本人确认" in exc.value.message

    with pytest.raises(ApiError) as exc2:
        methods.mcp_decide_item(
            db_session, settings, user_id=users["init"].id,
            matter_id=matter.id,
            payload={"decision": "approved", "expected_version": 1},
        )
    assert exc2.value.status_code == 403
    assert exc2.value.message == exc.value.message  # 双通道同一守卫同一文案


def test_declare_item不可逆无理由_双通道422(client, db_session, settings,
                                          users):
    """裁定 2 同源修复：必填理由在所有可达通道生效（原 methods.py 在
    create_matter 之后才赋值 irreversible，唯一校验被绕过，实测 4/4）。"""
    payload = {"title": "删库？", "question": "要不要删", "background": "b",
               "participant_ids": [users["alice"].id, users["bob"].id],
               "irreversible": True}
    _t, plain = issue_token(db_session, user=users["init"], name="init")
    db_session.commit()

    resp = client.post("/api/items", json=payload,
                       headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"

    with pytest.raises(ApiError) as exc:
        methods.mcp_declare_item(db_session, settings,
                                 user_id=users["init"].id, payload=payload)
    assert exc.value.status_code == 422

    assert db_session.scalar(select(Matter)) is None  # 不落库


def test_declare_item不可逆带理由_落库留痕(client, db_session, users):
    payload = {"title": "删库？", "question": "要不要删", "background": "b",
               "participant_ids": [users["alice"].id, users["bob"].id],
               "irreversible": True,
               "irreversible_reason": "涉及生产数据删除"}
    _t, plain = issue_token(db_session, user=users["init"], name="init")
    db_session.commit()

    resp = client.post("/api/items", json=payload,
                       headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200, resp.json()
    assert resp.json()["irreversible"] is True

    db_session.expire_all()
    matter = db_session.get(Matter, resp.json()["matter_id"])
    assert matter.irreversible is True
    assert matter.irreversible_reason == "涉及生产数据删除"


# ---------- 顺带：max_rounds 遵循配置 ----------


def test_declare_item的max_rounds遵循settings(db_session, settings, users):
    """原 methods.py:777 硬编码 max_rounds=10，运维改 MAX_ROUNDS 兜不住
    agent 通道建的事项（柠檬果 P2 实测：同一 MAX_ROUNDS=1 环境下网页 1、
    REST 10）。改为读 settings.max_rounds。"""
    import dataclasses

    s3 = dataclasses.replace(settings, max_rounds=3)
    out = methods.mcp_declare_item(
        db_session, s3, user_id=users["init"].id,
        payload={"title": "T", "question": "Q", "background": "",
                 "participant_ids": [users["alice"].id, users["bob"].id]},
    )
    db_session.expire_all()
    matter = db_session.get(Matter, out["matter_id"])
    assert matter.max_rounds == 3

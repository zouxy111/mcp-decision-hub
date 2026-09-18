"""REST 通道限流回归（PRD 9.1，2026-09-18 补齐）。

缺陷：token/account 两层限流只写在 MCP 入口中间件（``mcp_server/auth.py``
第 65-83 行）与 MCP 工具层（``tools.py:83-89``）。REST 通道——``require_bearer``
加 ``routes_api``/``routes_agent_rest`` 共 15 个端点——**一处未接**，只有认证。
提交端点还会驱动轮次进而触发 LLM 调用，无配额 = 无上限刷账单。

本文件与 MCP 侧那批测试的差别：``test_mcp_rate_limit.py`` 开头自陈「小阈值
触发 429 的场景需独立 uvicorn 进程……留待手工冒烟」，而这里用 conftest 的
``client``（真实 ``create_app``，``app.state.limiter`` 就在手上）把小阈值场景
**全部自动化**。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.config import Settings
from hub.db.models import Round, Task
from hub.domain.digest import compute_stance_content_hash
from hub.domain.rate_limit import (
    rate_limit_key_account,
    rate_limit_key_token,
)
from tests.conftest import make_user

TOKEN_LIMIT = 3
ACCOUNT_LIMIT = 100
SUBMIT_LIMIT = 2


@pytest.fixture()
def settings(tmp_path):
    """小阈值 settings，覆盖 conftest 的默认值（client/session_factory 随之）。

    token 取 3、account 取 100，是为了单测 token 层时 account 层不抢先撞线；
    submit 取 2 且 < token，是为了单测提交层时 token 层不抢先撞线
    （一次提交只各计 1 次）。
    """
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        session_secret="test-secret",
        admin_username=None,
        admin_initial_password=None,
        llm_provider_name="DeepSeek（测试）",
        rate_limit_token_per_minute=TOKEN_LIMIT,
        rate_limit_account_per_minute=ACCOUNT_LIMIT,
        rate_limit_submit_per_minute=SUBMIT_LIMIT,
    )


@pytest.fixture()
def agent_case(client, db_session, settings):
    """一个参与人 + 有效令牌 + 一份可提交的待办任务。"""
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
    rnd = db_session.scalar(
        select(Round).where(Round.matter_id == matter.id))

    tokens = {}
    for user in (alice, bob):
        token_obj, plain = issue_token(db_session, user=user, name=user.username)
        tokens[user.username] = {"plain": plain, "id": token_obj.id}
    db_session.commit()

    task = db_session.scalar(
        select(Task).where(Task.round_id == rnd.id,
                           Task.assignee_id == alice.id))
    return {"matter": matter, "round": rnd, "alice": alice, "bob": bob,
            "tokens": tokens, "task": task, "limiter": client.app.state.limiter}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_配额内正常放行(client, agent_case):
    """3 次配额内，前 3 次必须 200——限流不能误伤正常流量。"""
    h = _h(agent_case["tokens"]["alice"]["plain"])
    for i in range(TOKEN_LIMIT):
        resp = client.get("/api/agent/tasks", headers=h)
        assert resp.status_code == 200, f"第 {i + 1} 次被误伤：{resp.text}"


def test_超过令牌配额_返回429带RetryAfter(client, agent_case):
    """第 4 次超限：429 + Retry-After + PRD 9.5 统一错误形状。"""
    h = _h(agent_case["tokens"]["alice"]["plain"])
    for _ in range(TOKEN_LIMIT):
        assert client.get("/api/agent/tasks", headers=h).status_code == 200

    resp = client.get("/api/agent/tasks", headers=h)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error_code"] == "RATE_LIMITED"
    assert "频繁" in body["message"]
    # PRD 9.1 要求回 Retry-After；必须是正整数秒
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None, "缺 Retry-After 头"
    assert int(retry_after) >= 1
    assert body["details"]["retry_after"] >= 1


def test_账号维度配额独立生效(client, agent_case):
    """account 层单独生效：白盒先把该账号配额打满，随后请求 429。"""
    user = agent_case["alice"]
    limiter = agent_case["limiter"]
    key = rate_limit_key_account(user.id)
    for _ in range(ACCOUNT_LIMIT):
        limiter.allow(key, limit=ACCOUNT_LIMIT)

    resp = client.get("/api/agent/tasks",
                      headers=_h(agent_case["tokens"]["alice"]["plain"]))
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "RATE_LIMITED"


def test_提交端点有独立的第三层限额(client, agent_case):
    """提交层（submit 2/min）先于 token 层（3/min）撞线，且报错文案可区分。

    注意次数账：一次提交在 token 层与 submit 层各计 1。第 3 次请求时
    token 恰好用满第 3 格（放行）、submit 超第 3 格（拦下）——所以第 3 次
    的 429 必定来自**提交层**，文案里要能看出来。
    """
    h = _h(agent_case["tokens"]["alice"]["plain"])
    url = f"/api/agent/tasks/{agent_case['task'].id}/outputs"

    resps = [client.post(url, json={}, headers=h)
             for _ in range(SUBMIT_LIMIT + 1)]
    codes = [r.status_code for r in resps]

    assert 429 not in codes[:SUBMIT_LIMIT], f"配额内被误伤：{codes}"
    limit_resp = resps[SUBMIT_LIMIT]
    assert limit_resp.status_code == 429, f"第 {SUBMIT_LIMIT + 1} 次未限流：{codes}"
    assert limit_resp.json()["error_code"] == "RATE_LIMITED"
    assert "提交" in limit_resp.json()["message"], "提交层文案应与逐请求层可区分"


def test_换通道绕不过配额(client, agent_case):
    """MCP 入口先消耗掉的配额，REST 通道必须看见（共用 limiter + 同一套 key）。

    这是本次补齐的关键语义：若各通道各记一份账，换个通道就能翻倍配额。
    """
    alice = agent_case["alice"]
    tok_id = agent_case["tokens"]["alice"]["id"]
    limiter = agent_case["limiter"]

    # 模拟 MCP 入口中间件（mcp_server/auth.py:69-71）已经把令牌配额打满
    token_key = rate_limit_key_token(tok_id)
    for _ in range(TOKEN_LIMIT):
        ok, _ = limiter.allow(token_key, limit=TOKEN_LIMIT)
        assert ok is True

    resp = client.get("/api/agent/tasks",
                      headers=_h(agent_case["tokens"]["alice"]["plain"]))
    assert resp.status_code == 429, "MCP 已用尽的配额在 REST 侧未被计入"
    assert resp.json()["error_code"] == "RATE_LIMITED"
    # 且账号维度尚未用尽——说明拦下来的确实是被共享的那本账
    ok, _ = limiter.allow(rate_limit_key_account(alice.id),
                          limit=ACCOUNT_LIMIT)
    assert ok is True


def test_无效令牌仍是401不被限流改写(client, agent_case):
    """认证先于限流：反复用坏令牌试，永远是 401，不会变成 429。

    否则攻击者可以用 429/401 的差异判断「令牌是否存在」。
    """
    h = _h("definitely-not-a-real-token")
    for _ in range(TOKEN_LIMIT + 3):
        resp = client.get("/api/agent/tasks", headers=h)
        assert resp.status_code == 401, f"坏令牌被改写成 {resp.status_code}"
        assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


def test_一个令牌打满不影响另一个令牌(client, agent_case):
    """配额按令牌隔离：alice 撞线不该波及 bob。"""
    alice_h = _h(agent_case["tokens"]["alice"]["plain"])
    bob_h = _h(agent_case["tokens"]["bob"]["plain"])

    for _ in range(TOKEN_LIMIT):
        assert client.get("/api/agent/tasks", headers=alice_h).status_code == 200
    assert client.get("/api/agent/tasks", headers=alice_h).status_code == 429

    assert client.get("/api/agent/tasks", headers=bob_h).status_code == 200


# ---------------------------------------------------------------------------
# 豁免守卫。2026-09-18 补限流时，通用配额把下面两个入口的连发场景拦死了，
# 与 owner 既有裁定冲突（全量测试实测暴露）。按「既有裁定优先」给它们开了
# 豁免口（deps.require_bearer_unlimited），这里钉住别再被收回去。
#
# 与既有守卫的分工：既有测试用 101/110 次连发，依赖具体数字、盯的是「没有
# 业务层配额」；这里用 token=3 的小阈值，3~5 次就能验出「通用配额有没有
# 重新盖上来」，对阈值变化更敏感。
# ---------------------------------------------------------------------------

def _ask_stance_payload(*, target_id: int, round_number: int) -> dict:
    """定向提问的立场载荷。

    round_number 必须逐次递增：stances 有 (matter_id, round_number, user_id)
    唯一约束，同一人同一轮只能有一条立场。既有裁定测试同此处理。
    """
    data = {
        "round_number": round_number, "stance": "support", "confidence": 0.6,
        "position_summary": "支持", "rationale_summary": "需要对方补充数据",
        "non_negotiables": [], "conditions": [], "open_questions": [],
        "depends_on": [],
        "questions_for": [{"participant_id": str(target_id),
                           "question": "上线窗口是哪天？"}],
        "disagreement_kind": None, "supersedes": None,
        "acting_as": "human", "authority": None, "ttl_seconds": None,
        "urgency": "normal", "visibility": "participants",
    }
    data["content_hash"] = compute_stance_content_hash(data)
    return data


def test_定向提问端点豁免通用配额(client, agent_case):
    """POST /items/{id}/stances 的定向提问不受 token 配额约束。

    来源：owner 2026-09-16 更正裁定 1「定向提问不设配额」（撤销 09-14 的
    (matter, actor, target) 滑窗 N=100）。
    """
    h = _h(agent_case["tokens"]["alice"]["plain"])
    mid = agent_case["matter"].id
    target = agent_case["bob"].id

    for n in range(1, TOKEN_LIMIT + 3):
        resp = client.post(f"/api/items/{mid}/stances",
                           json=_ask_stance_payload(target_id=target,
                                                    round_number=n),
                           headers=h)
        assert resp.status_code == 201, (
            f"第 {n} 次定向提问被拦（token 阈值 {TOKEN_LIMIT}）："
            f"{resp.status_code} {resp.text[:200]}")


def test_ask端点豁免通用配额(client, agent_case):
    """POST /items/{id}/ask 不受 token 配额约束。

    来源：rpQt6D 验收第 4 条「不存在针对 ask 的计数/限流」。
    """
    h = _h(agent_case["tokens"]["alice"]["plain"])
    mid = agent_case["matter"].id
    target = agent_case["bob"].id

    for i in range(TOKEN_LIMIT + 2):
        resp = client.post(f"/api/items/{mid}/ask",
                           json={"target_user_id": target,
                                 "question": f"第 {i} 问"},
                           headers=h)
        assert resp.status_code == 200, (
            f"第 {i} 次 ask 被拦（token 阈值 {TOKEN_LIMIT}）："
            f"{resp.status_code} {resp.text[:200]}")

"""REST agent 通道「提交即驱动」回归（2026-09-18 实测发现）。

缺陷：`routes_agent_rest.rest_submit_output` 与 MCP 工具 `submit_output`
（tools.py:103-106）共用同一个 `methods.mcp_submit_output`，但**只写库、
不入队**——MCP 侧那句 `maybe_drive_round` + `drive_queue.put_nowait` 是
2026-08-12（2380632）加的，REST 通道（f27c8c6）后建时没跟着搬。

后果（实测，非推断）：参与人全部经 REST 提交产出后，轮次停在 `open`、
事项停在 `collecting`，8 秒轮询 0 次推进。且**重启也救不回来**——
`find_interrupted_round_ids` 的 (b) 分支只扫 `status == "in_progress"` 的
事项，且 (b) 要求「最新轮处于 generating/open/awaiting_summary」才算活跃
轮次之外的中断；本情形最新轮就是 open，两个条件都不命中；timeout_worker
也只扫 `pending` 任务（此刻任务已是 submitted）。比 2026-09-17 那次的
P0（拍板不入队，重启可恢复）更严重。

与裁定 1（2026-09-17：REST 拍板不入队）同源同型，故按同一口径修。
"""

import time

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import Matter, Resolution, Round, Task
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from tests.conftest import make_user

SUMMARY_CONVERGED = {
    "consensus_points": ["选方案 A"], "divergences": [],
    "blind_spots": [], "open_questions": [], "convergence": "converged",
}
DRAFT = {
    "recommendation": "采用方案 A", "rationale": "已收敛", "risks": ["进度"],
    "divergences": [], "cited_rounds": [1],
}


@pytest.fixture()
def app_llm(make_fake_llm):
    """本文件要跑完后台管线（收齐 → 摘要 → 草案），喂脚本化假 LLM。"""
    return make_fake_llm([SUMMARY_CONVERGED, DRAFT])


@pytest.fixture()
def scenario(client, db_session):
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
        _t, plain = issue_token(db_session, user=user, name=user.username)
        tokens[user.username] = plain
    db_session.commit()
    return {"matter": matter, "round": rnd, "alice": alice, "bob": bob,
            "tokens": tokens}


def _payload(rnd, key):
    answers = [{"question_id": q["question_id"], "content": "同意，按方案 A"}
               for q in rnd.questions]
    return {
        "answers": answers, "notes": None, "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, None),
        "idempotency_key": key,
    }


def _poll(session_factory, round_id, *, target, timeout=10.0):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        with session_factory() as s:
            st = s.get(Round, round_id).status
        if not seen or seen[-1] != st:
            seen.append(st)
        if st == target:
            break
        time.sleep(0.1)
    return seen


def test_REST提交产出后轮次自动收齐并推进(client, session_factory, db_session,
                                   scenario):
    """两个参与人全走 REST，收齐后必须由通道自己驱动到 closed。

    只断言「不在 open」是不够的：那条断言在「有别人恰好驱动过一次」
    的情况下也会绿。这里钉住完整终态：轮次 closed + 草案落地。
    """
    s = scenario
    rnd = s["round"]
    assert rnd.status == "open"

    for username in ("alice", "bob"):
        task = db_session.scalar(
            select(Task).where(Task.round_id == rnd.id,
                               Task.assignee_id == s[username].id))
        resp = client.post(
            f"/api/agent/tasks/{task.id}/outputs",
            json=_payload(rnd, f"rest-drive-{username}"),
            headers={"Authorization": f"Bearer {s['tokens'][username]}"},
        )
        assert resp.status_code == 201, resp.text

    seen = _poll(session_factory, rnd.id, target="closed")
    with session_factory() as s2:
        final_round = s2.get(Round, rnd.id).status
        final_matter = s2.get(Matter, s["matter"].id).status
        resolution = s2.scalar(select(Resolution))

    assert final_round == "closed", (
        f"REST 通道提交后轮次未收齐（轨迹 {seen}）："
        "rest_submit_output 缺 maybe_drive_round + drive_queue 接线")
    # 摘要=converged → _draft_resolution_phase 直接翻 awaiting_decision 并停在
    # 决议闸门（与 tests/integration/test_resolution_e2e.py 场景 1 同一终态）
    assert final_matter == "awaiting_decision", f"事项状态={final_matter}"
    assert resolution is not None
    assert resolution.status == "pending_review"

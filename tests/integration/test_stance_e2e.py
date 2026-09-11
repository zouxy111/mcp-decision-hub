"""立场层端到端串联（一条测试串起五个环节）。

链路：HTTP(Bearer) 提交 -> HTTP 读取+权限 -> 落库(Stance) -> 收敛判定
(convergence_eval) -> 差异化分发(audience)。

刻意不复用 tests/api/test_stances_api.py 的断言，也不把已有测试重跑一遍：
这里验证的是「五个环节能不能在同一套代码里串起来」，重点是接缝，尤其是
convergence_eval 用 str 作 user_id、audience 用 int 作 user_id 的类型转换。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import AuditEvent, MatterParticipant, Stance
from hub.domain.audience import FactBase, build_audience_view, scan_view
from hub.domain.convergence_eval import StanceInput, evaluate_convergence
from tests.conftest import make_user


def _headers(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _payload(**overrides) -> dict:
    data = {
        "round_number": 1,
        "stance": "support",
        "confidence": 0.6,
        "position_summary": "支持",
        "rationale_summary": "理由",
        "non_negotiables": [],
        "conditions": [],
        "open_questions": [],
        "depends_on": [],
        "questions_for": [],
        "disagreement_kind": None,
        "supersedes": None,
        "acting_as": "agent_on_behalf",
        "authority": "CFO",
        "ttl_seconds": 3600,
        "urgency": "normal",
        "visibility": "participants",
        "content_hash": "e" * 64,
    }
    data.update(overrides)
    return data


def test_立场层端到端从提交到差异化分发(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    dave = make_user(db_session, "dave")  # 非参与方

    # 标题刻意不含数字：出口扫描按纯文本匹配整数，标题里的 "v2" 会被误判成
    # 「未授权用户 id: 2」，见本文件末尾的已知缺陷用例。
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="是否上线推荐系统",
        goal="就是否上线达成结论", background="背景",
        participant_ids=[alice.id, bob.id], initiator_participates=True,
        timeout_seconds=3600, max_rounds=10, draft_questions=[],
    )
    _token_a, plain_a = issue_token(db_session, user=alice, name="alice-agent")
    _token_b, plain_b = issue_token(db_session, user=bob, name="bob-agent")
    _token_d, plain_d = issue_token(db_session, user=dave, name="dave-agent")
    db_session.commit()

    # —— 环节 2/3：HTTP 传输 + 立场模型落库 ——
    resp = client.post(
        f"/api/items/{matter.id}/stances",
        json=_payload(stance="support", confidence=0.9,
                      position_summary="支持上线", rationale_summary="收益明确"),
        headers=_headers(plain_a),
    )
    assert resp.status_code == 201, resp.text
    stance_id_alice = resp.json()["stance_id"]

    resp = client.post(
        f"/api/items/{matter.id}/stances",
        json=_payload(stance="oppose", confidence=0.8,
                      disagreement_kind="risk_appetite",
                      position_summary="反对上线", rationale_summary="风险不可控",
                      non_negotiables=["必须有人对风险兜底"]),
        headers=_headers(plain_b),
    )
    assert resp.status_code == 201, resp.text
    stance_id_bob = resp.json()["stance_id"]

    # —— 环节 4：权限（参与方可读、非参与方 404）+ 审计 ——
    resp = client.get(f"/api/items/{matter.id}/stances/{bob.id}",
                      headers=_headers(plain_a))
    assert resp.status_code == 200
    assert resp.json()["stance_id"] == stance_id_bob

    resp = client.get(f"/api/items/{matter.id}/stances/{bob.id}",
                      headers=_headers(plain_d))
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    db_session.expire_all()
    reads = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "stance_read")
    ).all()
    assert [(e.actor_user_id, e.matter_id) for e in reads] == [
        (alice.id, matter.id)
    ]

    rows = db_session.scalars(
        select(Stance).where(Stance.matter_id == matter.id)
    ).all()
    assert {s.stance_id for s in rows} == {stance_id_alice, stance_id_bob}
    assert len(db_session.scalars(select(MatterParticipant)).all()) == 2

    # —— 环节 5 前段：收敛判定（int user_id -> str user_id 显式转换）——
    inputs = [
        StanceInput(
            user_id=str(row.user_id),
            stance=row.stance,
            confidence=row.confidence,
            non_negotiables=tuple(row.non_negotiables),
            conditions=tuple(row.conditions),
            disagreement_kind=row.disagreement_kind,
        )
        for row in rows
    ]
    result = evaluate_convergence(inputs)
    assert result.converged is False
    assert {d.kind for d in result.divergences} == {"risk_appetite"}
    assert result.blocking
    # support=1.0、oppose=0.0 的立场权重均值；分数只作展示，不作判据
    assert result.agreement_score == pytest.approx(0.5)

    # —— 环节 5 后段：差异化分发（同一事实基座 -> 两份不同视图）——
    support_row = next(r for r in rows if r.user_id == alice.id)
    oppose_row = next(r for r in rows if r.user_id == bob.id)
    fact = FactBase(
        fact_version=f"{matter.id}:r{support_row.round_number}",
        item_title=matter.title,
        question=matter.goal,
        decision=None,
        rationale="收益明确，但风险尚未有兜底方案",
        supporting_user_ids=[support_row.user_id],
        opposing_user_ids=[oppose_row.user_id],
        risks=["风险不可控"],
        action_items={alice.id: ["补充风险兜底方案"], bob.id: []},
        dissenting={bob.id: oppose_row.position_summary},
    )
    view_alice = build_audience_view(fact, alice.id)
    view_bob = build_audience_view(fact, bob.id)

    assert view_alice.sections != view_bob.sections
    assert view_alice.fact_version == view_bob.fact_version == fact.fact_version
    assert "为什么这次没有采纳" in view_bob.sections
    assert "为什么这次没有采纳" not in view_alice.sections
    assert view_bob.sections["你当初的意见"] == "反对上线"

    assert scan_view(fact, view_alice).blocked is False
    assert scan_view(fact, view_bob).blocked is False


def test_出口扫描把标题里的版本号误判成未授权用户id_已知缺陷():
    """已知缺陷（记录当前真实行为，不是期望行为）。

    scan_view 用 `(?<!\\d)\\d+(?!\\d)` 在成品文本里找整数，任何裸整数只要
    等于某个参与人 id 且不在 visible_user_ids 里就被判为「泄漏用户 id」。
    真实参与人 id 是自增小整数（1、2、3…），于是 "推荐系统 v2"、"3 个方案"
    这种极普通的中文文案都会被误判成泄漏，把本来合法的消息拦下。

    这不是接线引入的问题，是 hub/domain/audience.py:scan_view 本身的
    假阳性，端到端串联把它暴露了出来。
    """
    fact = FactBase(
        fact_version="v1", item_title="是否上线推荐系统 v2",
        question="就是否上线达成结论", decision=None,
        rationale="收益明确，但风险尚未有兜底方案",
        supporting_user_ids=[1], opposing_user_ids=[2],
        risks=["风险不可控"], action_items={1: ["补充风险兜底方案"], 2: []},
        dissenting={2: "反对上线"},
    )
    view_alice = build_audience_view(fact, 1)

    result = scan_view(fact, view_alice)
    assert result.blocked is True
    assert result.violations == ["出现了未授权的用户 id: 2"]

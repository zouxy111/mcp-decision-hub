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
from hub.domain.audience import (
    SCAN_LIMITATIONS,
    FactBase,
    build_audience_view,
    scan_view,
)
from hub.domain.convergence_eval import StanceInput, evaluate_convergence
from hub.domain.digest import compute_stance_content_hash
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
        "authority": "can_commit",
        "ttl_seconds": 3600,
        "urgency": "normal",
        "visibility": "participants",
    }
    data.update(overrides)
    # 未显式指定 content_hash 时按正文重算，保证默认载荷是「自洽」的；
    # 显式传 content_hash 时原样保留，用于构造摘要不匹配的负例。
    if "content_hash" not in overrides:
        data["content_hash"] = compute_stance_content_hash(data)
    return data


def test_立场层端到端从提交到差异化分发(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    dave = make_user(db_session, "dave")  # 非参与方

    # 标题里的数字是刻意保留的：它曾经触发 scan_view 的假阳性（把 "v2" 当成
    # 「未授权用户 id: 2」），现在必须带着数字安然通过整条链路。
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="是否上线推荐系统 v2",
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
    assert view_bob.sections["你当初的意见"] == "反对上线"

    # A3：本轮 decision 为 None（还没形成结论）。此时对反对者既不能说
    # 「为什么这次没有采纳」，也不能紧接着解释「为什么这么定」——那都是在
    # 无结论的前提下假装有结论依据。改发「现在还没有结论」，如实说明意见
    # 仍在桌面上。
    assert "为什么这次没有采纳" not in view_bob.sections
    assert "现在还没有结论" in view_bob.sections
    assert "为什么这么定" not in view_bob.sections
    # 支持者本来就不该收到「未采纳」解释（角色差异，与是否成结论无关）。
    assert "为什么这次没有采纳" not in view_alice.sections

    # 出口扫描：标题带数字也照样放行；且「没发现问题」与「能发现什么」分开表达。
    result_alice = scan_view(fact, view_alice)
    result_bob = scan_view(fact, view_bob)
    assert result_alice.blocked is False
    assert result_bob.blocked is False
    assert result_alice.violations == []
    assert result_bob.violations == []
    assert result_alice.limitations == SCAN_LIMITATIONS
    assert result_bob.limitations == SCAN_LIMITATIONS


def test_标题里的版本号不再被判定为泄漏():
    """标题里的 "v2" 不再触发出口扫描的假阳性。

    scan_view 只在成品文本里认「id 形态」（如「用户 2」「user_id=2」），裸整数
    一概不判——权限判定本就由结构化数据（visible_user_ids）负责，正则不再承担
    这个职责。于是 "推荐系统 v2"、"3 个方案" 这类普通中文文案可以安然通过，
    同时 "用户2" 这种真实泄漏仍会被抓住（见 tests/domain/test_audience.py）。

    前提断言必须保留：先确认构造出的文本里确实含 "v2"，否则这条用例会退化成
    「一段不含数字的文本没被拦」——那什么都证明不了。
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

    # 前提：成品文本里确实含 "v2"，其中数字 2 恰好等于一位参与人的 id。
    assert "v2" in view_alice.sections["决定事项"]

    result = scan_view(fact, view_alice)
    assert result.blocked is False
    assert result.violations == []

"""立场 JSON API 骨架测试。

覆盖 Bearer 认证边界（缺头/合法/已吊销）、合法提交落库、非法载荷 422。
"""

import hashlib
import json

import pytest
from sqlalchemy import select, text

from hub.api.errors import ApiError
from hub.api.stances import analyze_round, to_fact_base, to_stance_inputs
from hub.api.tokens import issue_token, revoke_token
from hub.db.models import AuditEvent, Matter, MatterParticipant, Stance
from hub.domain.convergence_eval import StanceInput, evaluate_convergence
from hub.domain.digest import compute_stance_content_hash
from tests.conftest import make_user


def _stance_row(*, matter_id, user_id, stance, confidence=0.6,
                non_negotiables=None, conditions=None, disagreement_kind=None,
                position_summary="p") -> Stance:
    """内存中的 Stance 行（不落库），用于装配接缝的单元测试。"""
    return Stance(
        matter_id=matter_id, user_id=user_id, round_number=1, stance=stance,
        confidence=confidence, position_summary=position_summary,
        rationale_summary="r", non_negotiables=list(non_negotiables or []),
        conditions=list(conditions or []), open_questions=[], depends_on=[],
        questions_for=[], disagreement_kind=disagreement_kind, supersedes=None,
        acting_as="human", authority=None, ttl_seconds=None, urgency="normal",
        visibility="participants", content_hash="c" * 64,
    )


def _payload(**overrides) -> dict:
    data = {
        "round_number": 1,
        "stance": "support",
        "confidence": 0.6,
        "position_summary": "支持",
        "rationale_summary": "理由",
        "non_negotiables": ["底线"],
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
    }
    data.update(overrides)
    # 未显式指定 content_hash 时按正文重算，保证默认载荷是「自洽」的；
    # 显式传 content_hash 时原样保留，用于构造摘要不匹配的负例。
    if "content_hash" not in overrides:
        data["content_hash"] = compute_stance_content_hash(data)
    return data


def _bearer(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _make_matter(db_session, user, *, extra_participants=()) -> Matter:
    """事项 + 参与人花名册。user 自身默认是参与人（发起人自答）。"""
    matter = Matter(initiator_id=user.id, title="T", goal="G", background="B",
                    initiator_participates=True)
    db_session.add(matter)
    db_session.flush()
    for participant in (user, *extra_participants):
        db_session.add(MatterParticipant(matter_id=matter.id,
                                         user_id=participant.id))
    db_session.flush()
    return matter


def _make_matter_for_initiator(db_session, initiator, *, participants) -> Matter:
    """发起人本人不参与的事项（initiator_participates=False）。"""
    matter = Matter(initiator_id=initiator.id, title="T", goal="G", background="B",
                    initiator_participates=False)
    db_session.add(matter)
    db_session.flush()
    for participant in participants:
        db_session.add(MatterParticipant(matter_id=matter.id,
                                         user_id=participant.id))
    db_session.flush()
    return matter


def _auth_headers(db_session, user, name="a") -> dict:
    _token, plaintext = issue_token(db_session, user=user, name=name)
    db_session.commit()
    return _bearer(plaintext)


def test_无授权头提交立场返回401(client):
    resp = client.post("/api/items/mat_x/stances", json=_payload())
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


def test_合法bearer在路由内解析出当前用户(client, db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    resp = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                       headers=headers)
    assert resp.status_code == 201
    assert resp.json()["user_id"] == alice.id


def test_已吊销token提交立场返回401(client, db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    token, plaintext = issue_token(db_session, user=alice, name="a")
    revoke_token(db_session, user=alice, token_id=token.id)
    db_session.commit()

    resp = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                       headers=_bearer(plaintext))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


def test_提交合法立场落库恰好一行并返回stance_id(client, db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    resp = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                       headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["stance_id"].startswith("stn_")
    assert body["matter_id"] == matter.id
    assert body["stance"] == "support"

    db_session.expire_all()
    rows = db_session.scalars(select(Stance)).all()
    assert len(rows) == 1
    assert rows[0].stance_id == body["stance_id"]
    assert rows[0].user_id == alice.id
    assert rows[0].confidence == 0.6


def test_参与方能读到另一参与方提交的立场(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_bob = _auth_headers(db_session, bob, "bob")

    created = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                          headers=h_bob)
    assert created.status_code == 201

    resp = client.get(f"/api/items/{matter.id}/stances/{bob.id}",
                      headers=h_alice)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == bob.id
    assert body["stance_id"] == created.json()["stance_id"]


def test_非参与方读取立场返回404(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")  # 既不是发起人也不是参与人
    matter = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.commit()
    h_bob = _auth_headers(db_session, bob, "bob")
    h_carol = _auth_headers(db_session, carol, "carol")

    created = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                          headers=h_bob)
    assert created.status_code == 201

    resp = client.get(f"/api/items/{matter.id}/stances/{bob.id}",
                      headers=h_carol)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_读取不存在的参与人立场返回404(client, db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")

    resp = client.get(f"/api/items/{matter.id}/stances/999999",
                      headers=h_alice)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_跨事项读取立场返回404(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter_a = _make_matter(db_session, alice, extra_participants=(bob,))
    matter_b = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_bob = _auth_headers(db_session, bob, "bob")

    # bob 只在事项 B 提交立场
    created = client.post(f"/api/items/{matter_b.id}/stances", json=_payload(),
                          headers=h_bob)
    assert created.status_code == 201

    # 用事项 A 的 id + bob 读 -> A 里 bob 没有立场
    resp = client.get(f"/api/items/{matter_a.id}/stances/{bob.id}",
                      headers=h_alice)
    assert resp.status_code == 404


def test_读取立场写入审计事件(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_bob = _auth_headers(db_session, bob, "bob")

    created = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                          headers=h_bob)
    assert created.status_code == 201

    resp = client.get(f"/api/items/{matter.id}/stances/{bob.id}",
                      headers=h_alice)
    assert resp.status_code == 200

    db_session.expire_all()
    reads = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "stance_read")
    ).all()
    assert len(reads) == 1
    assert reads[0].actor_user_id == alice.id
    assert reads[0].matter_id == matter.id
    assert reads[0].detail["target_user_id"] == bob.id


def test_发起人未自答也能读到参与人可见的立场(client, db_session):
    """A2（PRD 第 3 章角色表）：发起人可见本事项全部摘要与原始回答。

    发起人 carol 未勾选参与（不在 matter_participants 里），仍必须能读到
    参与人 alice 提交的 visibility=participants 立场。
    """
    carol = make_user(db_session, "carol")   # 发起人，但不参与
    alice = make_user(db_session, "alice")
    matter = _make_matter_for_initiator(db_session, carol, participants=(alice,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_carol = _auth_headers(db_session, carol, "carol")

    created = client.post(f"/api/items/{matter.id}/stances",
                          json=_payload(visibility="participants"),
                          headers=h_alice)
    assert created.status_code == 201

    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_carol)
    assert resp.status_code == 200
    assert resp.json()["user_id"] == alice.id


def test_单人读取同样按可见性过滤(client, db_session):
    """单人端点不能成为绕过成员闸门的旁路。

    A2 后：成员（发起人 ∪ 参与人）内不再按 visibility 细分，但非成员仍 404。
    """
    carol = make_user(db_session, "carol")  # 发起人，但不参与
    alice = make_user(db_session, "alice")
    dave = make_user(db_session, "dave")    # 纯陌生人
    matter = _make_matter_for_initiator(db_session, carol,
                                        participants=(alice,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_carol = _auth_headers(db_session, carol, "carol")
    h_dave = _auth_headers(db_session, dave, "dave")

    created = client.post(f"/api/items/{matter.id}/stances",
                          json=_payload(visibility="participants"),
                          headers=h_alice)
    assert created.status_code == 201

    # 参与人 alice 自己读得到
    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_alice)
    assert resp.status_code == 200

    # A2：发起人 carol 未自答也读得到（PRD 第 3 章：发起人可见全部原始回答）
    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_carol)
    assert resp.status_code == 200
    assert resp.json()["user_id"] == alice.id

    # 纯陌生人 dave 仍 404 —— 成员闸门没有被放开
    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_dave)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_读取失败不写审计(client, db_session):
    """404 的读取不应留下 stance_read 审计（否则审计被噪声淹没）。"""
    alice = make_user(db_session, "alice")
    carol = make_user(db_session, "carol")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    h_carol = _auth_headers(db_session, carol, "carol")

    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_carol)
    assert resp.status_code == 404

    db_session.expire_all()
    assert db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "stance_read")
    ).all() == []


def test_立场列表只返回本事项且按可见性过滤(client, db_session):
    carol = make_user(db_session, "carol")  # 发起人，但不参与
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    dave = make_user(db_session, "dave")    # 纯陌生人
    matter_a = _make_matter_for_initiator(db_session, carol,
                                          participants=(alice, bob))
    matter_b = _make_matter_for_initiator(db_session, carol,
                                          participants=(alice, bob))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_bob = _auth_headers(db_session, bob, "bob")
    h_carol = _auth_headers(db_session, carol, "carol")
    h_dave = _auth_headers(db_session, dave, "dave")

    a1 = client.post(f"/api/items/{matter_a.id}/stances",
                     json=_payload(visibility="participants"), headers=h_alice)
    a2 = client.post(f"/api/items/{matter_a.id}/stances",
                     json=_payload(visibility="all"), headers=h_bob)
    b1 = client.post(f"/api/items/{matter_b.id}/stances", json=_payload(),
                     headers=h_bob)
    assert (a1.status_code, a2.status_code, b1.status_code) == (201, 201, 201)

    # 参与人 alice：只拿到本事项 A 的两条，不含事项 B 的
    resp = client.get(f"/api/items/{matter_a.id}/stances", headers=h_alice)
    assert resp.status_code == 200
    body = resp.json()
    assert {s["stance_id"] for s in body} == {a1.json()["stance_id"],
                                              a2.json()["stance_id"]}
    assert {s["matter_id"] for s in body} == {matter_a.id}

    # A2：发起人 carol（非参与人）也能看到本事项两条 —— 成员内不再按 visibility 细分
    resp = client.get(f"/api/items/{matter_a.id}/stances", headers=h_carol)
    assert resp.status_code == 200
    assert {s["stance_id"] for s in resp.json()} == {a1.json()["stance_id"],
                                                    a2.json()["stance_id"]}

    # 纯陌生人：404（成员闸门未被放宽）
    resp = client.get(f"/api/items/{matter_a.id}/stances", headers=h_dave)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_非参与方提交立场返回404(client, db_session):
    alice = make_user(db_session, "alice")
    carol = make_user(db_session, "carol")   # 纯陌生人
    matter = _make_matter(db_session, alice)
    # 发起人但不参与的事项：发起人也不能提交立场
    matter2 = _make_matter_for_initiator(db_session, carol,
                                         participants=(alice,))
    db_session.commit()
    h_carol = _auth_headers(db_session, carol, "carol")

    for target in (matter, matter2):
        resp = client.post(f"/api/items/{target.id}/stances", json=_payload(),
                           headers=h_carol)
        # 关键：必须是 404（不是 500），否则说明外键 IntegrityError 漏出去了
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    db_session.expire_all()
    assert db_session.scalars(select(Stance)).all() == []


def test_提交到不存在的事项返回404(client, db_session):
    alice = make_user(db_session, "alice")
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")

    resp = client.post("/api/items/mat_不存在/stances", json=_payload(),
                       headers=h_alice)
    # 关键：必须是 404（不是 500）
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    db_session.expire_all()
    assert db_session.scalars(select(Stance)).all() == []


def test_装配函数从服务层导出():
    """A1：Stance 行 -> StanceInput / FactBase 的装配必须在生产模块里，不能只活在测试里。"""
    from hub.api.stances import to_fact_base, to_stance_inputs  # noqa: F401


def test_装配接缝封闭str与int转换(db_session):
    """B2 接缝：str/int 转换必须封在 to_stance_inputs / to_fact_base 内部。"""
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    rows = [
        _stance_row(matter_id="mat_x", user_id=alice.id, stance="support",
                    non_negotiables=["底线"], conditions=["若成本可控"]),
        _stance_row(matter_id="mat_x", user_id=bob.id, stance="oppose",
                    disagreement_kind="risk_appetite"),
    ]

    inputs = to_stance_inputs(rows)
    assert all(isinstance(i.user_id, str) for i in inputs)          # 接缝封闭
    assert [i.user_id for i in inputs] == [str(r.user_id) for r in rows]
    assert inputs[0].non_negotiables == ("底线",)                    # JSON 列 -> tuple
    assert inputs[0].conditions == ("若成本可控",)
    assert inputs[0].disagreement_kind is None

    fact = to_fact_base(rows=rows, fact_version="v1", item_title="T", question="Q",
                        decision=None, rationale="R", display_names={alice.id: "alice"})
    assert fact.supporting_user_ids == [alice.id]                    # 回到 int
    assert all(isinstance(u, int) for u in fact.supporting_user_ids)
    assert fact.opposing_user_ids == [bob.id]
    assert all(isinstance(u, int) for u in fact.opposing_user_ids)
    assert fact.dissenting == {bob.id: "p"}                          # 缺省从 oppose 派生
    assert fact.display_names == {alice.id: "alice"}
    assert fact.risks == [] and fact.action_items == {}


def test_装配接缝不做转换会切出不同的阵营(db_session):
    """对照证据：绕开 to_stance_inputs 直接拿 int 当 user_id，结果与契约不符。

    同一份「1 支持 + 1 反对」的数据：
    - 走 to_stance_inputs：阵营 user_ids 是 str，切分正确；
    - 直传 int：反对者分支在 join 中文描述时直接 TypeError 崩掉。
    若全是支持者（不触发 join），则静默产出 int 阵营 —— 类型与契约的 str
    不一致，下游任何 str 比对都会永远不命中。两条都说明接缝必须封死。
    """
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    rows = [
        _stance_row(matter_id="mat_x", user_id=alice.id, stance="support"),
        _stance_row(matter_id="mat_x", user_id=bob.id, stance="oppose"),
    ]

    correct = evaluate_convergence(to_stance_inputs(rows))
    assert {f.user_ids for f in correct.factions} == {(str(alice.id),), (str(bob.id),)}
    assert all(isinstance(u, str) for f in correct.factions for u in f.user_ids)

    naive = [StanceInput(user_id=r.user_id, stance=r.stance,
                         confidence=r.confidence) for r in rows]
    with pytest.raises(TypeError):
        evaluate_convergence(naive)

    # 全支持者时不崩，但静默产出 int 阵营 —— 契约是 str
    calm = [r for r in rows if r.stance == "support"]
    naive_calm = evaluate_convergence(
        [StanceInput(user_id=r.user_id, stance=r.stance, confidence=r.confidence)
         for r in calm]
    )
    assert {f.user_ids for f in naive_calm.factions} == {(alice.id,)}
    assert not all(
        isinstance(u, str) for f in naive_calm.factions for u in f.user_ids
    )


def test_content_hash与正文不匹配返回422且不落库(client, db_session):
    """B3：content_hash 必须服务端重算比对，客户端传什么不等于存什么。"""
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    resp = client.post(f"/api/items/{matter.id}/stances",
                       json=_payload(content_hash="0" * 64), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"

    db_session.expire_all()
    assert db_session.scalars(select(Stance)).all() == []


def _reference_hash(fields: dict) -> str:
    """独立按冻结口径重算（不复用生产函数，避免自证）。"""
    body = {k: v for k, v in fields.items() if k != "content_hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_content_hash自洽的载荷落库且原样返回(client, db_session):
    """正向路径：自洽的摘要不但要放行，还要原样回传（不能被服务端改写）。"""
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    payload = _payload()
    assert payload["content_hash"] == _reference_hash(payload)  # 口径锁定

    resp = client.post(f"/api/items/{matter.id}/stances", json=payload,
                       headers=headers)
    assert resp.status_code == 201
    assert resp.json()["content_hash"] == payload["content_hash"]


def test_改动正文沿用旧hash返回422(client, db_session):
    """口径锁定：hash 必须绑定正文，不是走过场。"""
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    payload = _payload()                       # hash 与正文自洽
    payload["position_summary"] = "改成了另一段正文"   # 正文改了，hash 没跟上

    resp = client.post(f"/api/items/{matter.id}/stances", json=payload,
                       headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"

    db_session.expire_all()
    assert db_session.scalars(select(Stance)).all() == []


def test_analyze_round非成员返回404(db_session):
    """成员闸门在 analyze_round 这一层同样生效，且文案与「事项不存在」一致。"""
    alice = make_user(db_session, "alice")
    dave = make_user(db_session, "dave")   # 纯陌生人
    matter = _make_matter(db_session, alice)
    db_session.commit()

    with pytest.raises(ApiError) as exc:
        analyze_round(db_session, matter_id=matter.id, round_number=1, user=dave)
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"


def test_analyze_round跨事项非成员返回404(db_session):
    """bob 只是事项 B 的参与人，读事项 A 的分析仍 404。"""
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter_a = _make_matter(db_session, alice)
    matter_b = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.commit()

    with pytest.raises(ApiError) as exc:
        analyze_round(db_session, matter_id=matter_a.id, round_number=1, user=bob)
    assert exc.value.status_code == 404
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"

    # 在自己参与的事项 B 上则放行
    analysis = analyze_round(db_session, matter_id=matter_b.id, round_number=1,
                             user=bob)
    assert analysis.fact_version == f"{matter_b.id}:r1"


def test_analyze_round返回含limitations与undetected_checks的分析(db_session):
    """C3 落点：分析结果必须如实带出扫描覆盖不到的范围与未检测检查。"""
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = _make_matter(db_session, alice, extra_participants=(bob,))
    db_session.add_all([
        _stance_row(matter_id=matter.id, user_id=alice.id, stance="support"),
        _stance_row(matter_id=matter.id, user_id=bob.id, stance="oppose"),
    ])
    db_session.commit()

    analysis = analyze_round(db_session, matter_id=matter.id, round_number=1,
                             user=alice)
    assert isinstance(analysis.limitations, tuple)
    assert analysis.limitations          # 出口扫描必须暴露它覆盖不到的范围
    assert isinstance(analysis.undetected_checks, tuple)
    assert isinstance(analysis.violations, list)
    assert analysis.converged is False          # 有反对者
    assert analysis.degrading is False
    assert analysis.skipped_user_ids == ()
    assert analysis.fact_version == f"{matter.id}:r1"
    assert set(analysis.sections)               # 视图非空
    assert analysis.sections["决定事项"] == matter.title


def test_analyze_round脏数据降级并逐条写审计(db_session):
    """宽容版跳过脏 stance 行：降级不许冒充收敛，且每个被跳过项留一条审计。

    真实 schema 带 CHECK (stance IN (...))，正常路径写不进脏行；这里用
    SQLite 的 ignore_check_constraints 模拟「历史遗留/外部导入的脏数据入库」
    —— 这正是宽容分支存在的理由。

    ignore_check_constraints 对应的真实场景是**存量库**：本仓库无迁移工具，
    CHECK 只对新建库生效，老库的 stances 表没有该约束。所以这不是在测一个
    假想分支。
    """
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    db_session.add(_stance_row(matter_id=matter.id, user_id=alice.id,
                               stance="banana"))
    db_session.commit()
    db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    analysis = analyze_round(db_session, matter_id=matter.id, round_number=1,
                             user=alice)
    assert analysis.degrading is True
    assert analysis.converged is False          # 脏数据不许冒充收敛
    assert analysis.skipped_user_ids == (str(alice.id),)

    db_session.expire_all()
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "convergence_degraded")
    ).all()
    assert len(events) == 1
    assert events[0].matter_id == matter.id
    assert events[0].detail == {"stance": "banana", "user_id": alice.id}


def test_分析接口返回本轮分析且复用成员闸门(client, db_session):
    """C3：GET /api/items/{matter_id}/stances/analysis 只读暴露 RoundStanceAnalysis。"""
    carol = make_user(db_session, "carol")   # 发起人，但不参与
    alice = make_user(db_session, "alice")
    dave = make_user(db_session, "dave")     # 纯陌生人
    matter = _make_matter_for_initiator(db_session, carol, participants=(alice,))
    db_session.add(_stance_row(matter_id=matter.id, user_id=alice.id,
                               stance="support"))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_carol = _auth_headers(db_session, carol, "carol")
    h_dave = _auth_headers(db_session, dave, "dave")

    resp = client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers=h_alice)
    assert resp.status_code == 200
    body = resp.json()
    assert body["fact_version"] == f"{matter.id}:r1"
    assert body["converged"] is True
    assert body["degrading"] is False
    assert isinstance(body["limitations"], list) and body["limitations"]
    assert isinstance(body["undetected_checks"], list)
    assert body["skipped_user_ids"] == []
    assert body["sections"]["决定事项"] == matter.title

    # 发起人（成员）同样可读
    assert client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers=h_carol).status_code == 200

    # 纯陌生人：404，语义与 stance 既有路由一致
    resp = client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers=h_dave)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_分析接口按轮次参数切换(client, db_session):
    """轮次由查询参数给出；空轮次不报错、如实判为未收敛。"""
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    round2 = _stance_row(matter_id=matter.id, user_id=alice.id, stance="oppose")
    round2.round_number = 2
    db_session.add_all([
        _stance_row(matter_id=matter.id, user_id=alice.id, stance="support"),
        round2,
    ])
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    first = client.get(f"/api/items/{matter.id}/stances/analysis?round_number=1",
                       headers=headers)
    assert first.status_code == 200
    assert first.json()["fact_version"] == f"{matter.id}:r1"
    assert first.json()["converged"] is True

    second = client.get(f"/api/items/{matter.id}/stances/analysis?round_number=2",
                        headers=headers)
    assert second.status_code == 200
    assert second.json()["fact_version"] == f"{matter.id}:r2"
    assert second.json()["converged"] is False      # 有反对者

    empty = client.get(f"/api/items/{matter.id}/stances/analysis?round_number=9",
                       headers=headers)
    assert empty.status_code == 200
    assert empty.json()["converged"] is False


def test_分析接口无授权头返回401(client):
    resp = client.get("/api/items/mat_x/stances/analysis")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.parametrize("overrides", [{"stance": "banana"}, {"confidence": 1.5}])
def test_非法载荷返回422且不落库(client, db_session, overrides):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.commit()
    headers = _auth_headers(db_session, alice)

    resp = client.post(f"/api/items/{matter.id}/stances",
                       json=_payload(**overrides), headers=headers)
    # 关键：必须是 422（不是 500），否则说明 raise 被当成未处理异常。
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_FAILED"

    db_session.expire_all()
    assert db_session.scalars(select(Stance)).all() == []


def test_分析接口在OpenAPI中声明响应模型(client, db_session):
    """B1 收口：analysis 端点必须声明 response_model，键集合进 API 契约，
    而不是只在运行时碰巧是 dict。"""
    carol = make_user(db_session, "carol")
    alice = make_user(db_session, "alice")
    matter = _make_matter_for_initiator(db_session, carol, participants=(alice,))
    db_session.add(_stance_row(matter_id=matter.id, user_id=alice.id,
                               stance="support"))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")

    resp = client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers=h_alice)
    assert resp.status_code == 200

    from hub.schemas.stance import StanceAnalysisOut
    StanceAnalysisOut.model_validate(resp.json())

    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/items/{matter_id}/stances/analysis"]["get"]
    content = op["responses"]["200"].get("content", {})
    assert "application/json" in content
    assert "StanceAnalysisOut" in content["application/json"]["schema"][
        "$ref"]

"""立场 JSON API 骨架测试。

覆盖 Bearer 认证边界（缺头/合法/已吊销）、合法提交落库、非法载荷 422。
"""

import pytest
from sqlalchemy import select

from hub.api.tokens import issue_token, revoke_token
from hub.db.models import AuditEvent, Matter, MatterParticipant, Stance
from tests.conftest import make_user


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
        "content_hash": "c" * 64,
    }
    data.update(overrides)
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


def test_单人读取同样按可见性过滤(client, db_session):
    """单人端点不能成为绕过可见性过滤的旁路。"""
    carol = make_user(db_session, "carol")  # 发起人，但不参与
    alice = make_user(db_session, "alice")
    matter = _make_matter_for_initiator(db_session, carol,
                                        participants=(alice,))
    db_session.commit()
    h_alice = _auth_headers(db_session, alice, "alice")
    h_carol = _auth_headers(db_session, carol, "carol")

    created = client.post(f"/api/items/{matter.id}/stances",
                          json=_payload(visibility="participants"),
                          headers=h_alice)
    assert created.status_code == 201

    # 参与人 alice 自己读得到
    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_alice)
    assert resp.status_code == 200

    # 发起人 carol 读不到仅参与人可见的立场
    resp = client.get(f"/api/items/{matter.id}/stances/{alice.id}",
                      headers=h_carol)
    assert resp.status_code == 404


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

    # 发起人 carol（非参与人）：participants 那条对她不可见，只看到 all 那条
    resp = client.get(f"/api/items/{matter_a.id}/stances", headers=h_carol)
    assert resp.status_code == 200
    assert [s["stance_id"] for s in resp.json()] == [a2.json()["stance_id"]]

    # 纯陌生人：404
    resp = client.get(f"/api/items/{matter_a.id}/stances", headers=h_dave)
    assert resp.status_code == 404


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

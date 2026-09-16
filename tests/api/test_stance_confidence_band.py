"""PRD-07 出参面的落地：`StanceRead` 只发分档，不发 `confidence` 原值。

为什么要能在**字典输入**上也生效：路由的 `response_model=` 喂的是 ORM 对象，
而测试与 MCP 侧都用 `StanceRead.model_validate(body)` 复验已序列化的 JSON。
两条路都必须把原值挡掉，否则堵了一半。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import Stance
from hub.domain.confidence import confidence_band
from hub.schemas.stance import StanceRead
from tests.conftest import make_user


def test_单读端点不发原值只发分档(client, db_session):
    """对真实 HTTP 响应断言键集合 —— 不看模型字段名，看调用方真拿到什么。"""
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="T", goal="G", background="B",
        participant_ids=[bob.id, make_user(db_session, "carol").id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    db_session.commit()
    _t, plain_bob = issue_token(db_session, user=bob, name="bob")
    db_session.commit()

    from hub.domain.digest import compute_stance_content_hash
    payload = {
        "round_number": 1, "stance": "support", "confidence": 0.95,
        "position_summary": "支持", "rationale_summary": "理由",
        "non_negotiables": [], "conditions": [], "open_questions": [],
        "depends_on": [], "questions_for": [], "disagreement_kind": None,
        "supersedes": None, "acting_as": "human", "authority": None,
        "ttl_seconds": None, "urgency": "normal", "visibility": "participants",
    }
    payload["content_hash"] = compute_stance_content_hash(payload)
    resp = client.post(f"/api/items/{matter.id}/stances", json=payload,
                       headers={"Authorization": f"Bearer {plain_bob}"})
    assert resp.status_code == 201, resp.text

    row = db_session.scalar(select(Stance).where(Stance.user_id == bob.id))
    resp = client.get(f"/api/items/{matter.id}/stances/{row.user_id}",
                      headers={"Authorization": f"Bearer {plain_bob}"})
    assert resp.status_code == 200
    body = resp.json()

    assert "confidence" not in body, "原值外发等于把「谁在卡结论」暴露出去"
    assert body["confidence_band"] == confidence_band(0.95) == "high"
    # 正文里也不能出现原值字样（0.95）
    assert "0.95" not in resp.text


def test_字典输入同样被挡掉原值():
    """`model_validate(body)` 这条路也必须转换 —— 两条路都要堵。"""
    base = StanceRead.model_fields
    assert "confidence" not in base
    assert "confidence_band" in base

    obj = StanceRead.model_validate({
        "stance_id": "stn_x", "matter_id": "mat_x", "round_number": 1,
        "user_id": 1, "stance": "support", "confidence": 0.2,
        "position_summary": "p", "rationale_summary": "r",
        "non_negotiables": [], "conditions": [], "open_questions": [],
        "depends_on": [], "questions_for": [], "disagreement_kind": None,
        "supersedes": None, "acting_as": "human", "authority": None,
        "ttl_seconds": None, "urgency": "normal", "visibility": "participants",
        "content_hash": "a" * 64, "created_at": "2026-09-17T00:00:00Z",
    })
    assert obj.confidence_band == "low"
    assert "confidence" not in obj.model_dump()


@pytest.mark.parametrize("value,expected", [(0.2, "low"), (0.6, "medium"),
                                            (0.9, "high")])
def test_三档都能到达(value, expected):
    obj = StanceRead.model_validate({
        "stance_id": "s", "matter_id": "m", "round_number": 1, "user_id": 1,
        "stance": "support", "confidence": value, "position_summary": "p",
        "rationale_summary": "r", "non_negotiables": [], "conditions": [],
        "open_questions": [], "depends_on": [], "questions_for": [],
        "disagreement_kind": None, "supersedes": None, "acting_as": "human",
        "authority": None, "ttl_seconds": None, "urgency": "normal",
        "visibility": "participants", "content_hash": "a" * 64,
        "created_at": "2026-09-17T00:00:00Z",
    })
    assert obj.confidence_band == expected

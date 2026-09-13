"""B27：立场列表端点裁剪私有字段。

断言一律对着真实 HTTP 响应（resp.json() 的键集合 / resp.text 的正文），
不对着模型字段名清单。
"""

from sqlalchemy import select

from hub.api.tokens import issue_token
from hub.db.models import Matter, MatterParticipant, Stance
from hub.domain.digest import compute_stance_content_hash
from tests.conftest import make_user

# 私有字段默认清单【待 owner 确认】：内部把握/依据类信息不进列表。
PRIVATE_FIELDS = {
    "confidence",
    "rationale_summary",
    "non_negotiables",
    "conditions",
    "disagreement_kind",
}

_SECRET_RATIONALE = "机密依据-甲乙方分成比例-7f3a"
_SECRET_BOTTOM_LINE = "不可谈底线-独家授权-b21c"
_SECRET_CONDITION = "秘密条件-先付定金-9d04"


def _payload(**overrides) -> dict:
    data = {
        "round_number": 1,
        "stance": "oppose",
        "confidence": 0.95,
        "position_summary": "反对按现方案上线",
        "rationale_summary": _SECRET_RATIONALE,
        "non_negotiables": [_SECRET_BOTTOM_LINE],
        "conditions": [_SECRET_CONDITION],
        "open_questions": [],
        "depends_on": [],
        "questions_for": [],
        "disagreement_kind": "goal",
        "supersedes": None,
        "acting_as": "human",
        "authority": None,
        "ttl_seconds": None,
        "urgency": "normal",
        "visibility": "participants",
    }
    data.update(overrides)
    data["content_hash"] = compute_stance_content_hash(data)
    return data


def _bearer(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _setup(client, db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = Matter(initiator_id=alice.id, title="T", goal="G", background="B",
                    initiator_participates=True)
    db_session.add(matter)
    db_session.flush()
    db_session.add(MatterParticipant(matter_id=matter.id, user_id=bob.id))
    db_session.flush()
    _t, plain_bob = issue_token(db_session, user=bob, name="bob")
    _t, plain_alice = issue_token(db_session, user=alice, name="alice")
    db_session.commit()

    resp = client.post(f"/api/items/{matter.id}/stances", json=_payload(),
                       headers=_bearer(plain_bob))
    assert resp.status_code == 201, resp.text
    return matter, plain_alice


def test_列表响应不含私有字段(client, db_session):
    matter, plain_alice = _setup(client, db_session)

    resp = client.get(f"/api/items/{matter.id}/stances",
                      headers=_bearer(plain_alice))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1

    # 键集合层面：五个私有字段一个都不许出现
    assert PRIVATE_FIELDS.isdisjoint(body[0])
    # 正文层面：私有内容一个字都不许漏出（对真实响应断言，不是对字段名）
    assert _SECRET_RATIONALE not in resp.text
    assert _SECRET_BOTTOM_LINE not in resp.text
    assert _SECRET_CONDITION not in resp.text

    # 公开字段仍在：列表仍然可用
    assert body[0]["stance"] == "oppose"
    assert body[0]["position_summary"] == "反对按现方案上线"
    assert body[0]["stance_id"].startswith("stn_")


def test_私有字段仍在库里且单读端点不受影响(client, db_session):
    """裁剪只发生在列表出参：数据照存，单读仍是全字段。"""
    matter, plain_alice = _setup(client, db_session)

    db_session.expire_all()
    row = db_session.scalars(select(Stance)).one()
    assert row.rationale_summary == _SECRET_RATIONALE

    bob_id = row.user_id
    resp = client.get(f"/api/items/{matter.id}/stances/{bob_id}",
                      headers=_bearer(plain_alice))
    assert resp.status_code == 200
    assert resp.json()["rationale_summary"] == _SECRET_RATIONALE

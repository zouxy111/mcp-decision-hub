"""Stance ORM 模型测试（立场层第一个垂直切片）。

只覆盖新增的 stances 表；不改动任何既有表。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.db.models import Matter, Stance
from tests.conftest import make_user


def _make_matter(db_session, user) -> Matter:
    matter = Matter(initiator_id=user.id, title="T", goal="G", background="B")
    db_session.add(matter)
    db_session.flush()
    return matter


def _stance_kwargs(matter, user, *, round_number=1, **overrides) -> dict:
    kwargs = {
        "matter_id": matter.id,
        "round_number": round_number,
        "user_id": user.id,
        "stance": "conditional",
        "confidence": 0.72,
        "position_summary": "有条件支持",
        "rationale_summary": "因为成本可控",
        "non_negotiables": ["合规底线"],
        "conditions": ["预算不超过 100 万"],
        "open_questions": ["谁负责验收？"],
        "depends_on": ["法务结论"],
        "questions_for": [{"participant_id": "u-2", "question": "你如何看风险？"}],
        "disagreement_kind": "risk_appetite",
        "supersedes": None,
        "acting_as": "agent_on_behalf",
        "authority": "CFO 授权",
        "ttl_seconds": 3600,
        "urgency": "high",
        "visibility": "all",
        "content_hash": "a" * 64,
    }
    kwargs.update(overrides)
    return kwargs


def test_完整立场对象落库读回字段无损(db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    stance = Stance(**_stance_kwargs(matter, alice))
    db_session.add(stance)
    db_session.commit()
    stance_id = stance.stance_id
    db_session.expire_all()

    loaded = db_session.scalar(select(Stance).where(Stance.stance_id == stance_id))
    assert loaded is not None
    assert loaded.stance_id == stance_id
    assert loaded.matter_id == matter.id
    assert loaded.round_number == 1
    assert loaded.user_id == alice.id
    assert loaded.stance == "conditional"
    assert loaded.confidence == 0.72
    assert loaded.position_summary == "有条件支持"
    assert loaded.rationale_summary == "因为成本可控"
    assert loaded.non_negotiables == ["合规底线"]
    assert loaded.conditions == ["预算不超过 100 万"]
    assert loaded.open_questions == ["谁负责验收？"]
    assert loaded.depends_on == ["法务结论"]
    assert loaded.questions_for == [
        {"participant_id": "u-2", "question": "你如何看风险？"}
    ]
    assert loaded.disagreement_kind == "risk_appetite"
    assert loaded.supersedes is None
    assert loaded.acting_as == "agent_on_behalf"
    assert loaded.authority == "CFO 授权"
    assert loaded.ttl_seconds == 3600
    assert loaded.urgency == "high"
    assert loaded.visibility == "all"
    assert loaded.content_hash == "a" * 64
    assert loaded.created_at is not None


def test_同一matter轮次用户重复提交被拦下(db_session):
    alice = make_user(db_session, "alice")
    matter = _make_matter(db_session, alice)
    db_session.add(Stance(**_stance_kwargs(matter, alice)))
    db_session.commit()

    db_session.add(Stance(**_stance_kwargs(matter, alice)))
    with pytest.raises(IntegrityError):
        db_session.flush()

"""rsEXuh ⑥：读取留痕的 **detail 键集合**（PRD-06）。

PRD-06 的验收原话：

> 调一次 analysis → `audit_events` 恰多一条 `audience_view_delivered`，
> detail **恰为** `{viewer_user_id, fact_version}` 两键
> detail 键集合被测试钉死（**多一个键就红**）

为什么单独写一条：这条验收**从来没有实现过**。2026-09-17 梳理「实现 vs 文档
口径不一致」时才发现整个 `tests/` 下搜 `audience_view_delivered` 零命中 ——
也正因如此，detail 里多出来的 `content_hash` 才能一直没人发现。
"""

from sqlalchemy import select

from hub.api import audit as audit_mod
from hub.api.tokens import issue_token
from hub.db.models import AuditEvent, Matter, MatterParticipant, Stance
from hub.domain.digest import compute_stance_content_hash
from tests.conftest import make_user

EXPECTED_KEYS = {"viewer_user_id", "fact_version"}


def _payload() -> dict:
    data = {
        "round_number": 1, "stance": "support", "confidence": 0.6,
        "position_summary": "支持", "rationale_summary": "理由",
        "non_negotiables": [], "conditions": [], "open_questions": [],
        "depends_on": [], "questions_for": [], "disagreement_kind": None,
        "supersedes": None, "acting_as": "human", "authority": None,
        "ttl_seconds": None, "urgency": "normal", "visibility": "participants",
    }
    data["content_hash"] = compute_stance_content_hash(data)
    return data


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
                       headers={"Authorization": f"Bearer {plain_bob}"})
    assert resp.status_code == 201, resp.text
    assert db_session.scalars(select(Stance)).first() is not None
    return matter, plain_alice


def _view_events(db_session, matter_id):
    return db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == audit_mod.AUDIENCE_VIEW_DELIVERED,
            AuditEvent.matter_id == matter_id,
        )
    ).all()


def test_一次analysis恰多一条留痕(client, db_session):
    matter, plain_alice = _setup(client, db_session)
    before = len(_view_events(db_session, matter.id))

    resp = client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers={"Authorization": f"Bearer {plain_alice}"})
    assert resp.status_code == 200

    db_session.expire_all()
    events = _view_events(db_session, matter.id)
    assert len(events) == before + 1
    assert events[-1].detail.keys() == EXPECTED_KEYS, (
        f"detail 键集合被文档钉死为两键，实际 {sorted(events[-1].detail)}"
    )


def test_detail不含视图内容(client, db_session):
    """「不记视图内容」：detail 里的值只能是 id 与版本号，不能夹带正文。"""
    matter, plain_alice = _setup(client, db_session)
    client.get(f"/api/items/{matter.id}/stances/analysis",
               headers={"Authorization": f"Bearer {plain_alice}"})

    db_session.expire_all()
    detail = _view_events(db_session, matter.id)[-1].detail
    assert isinstance(detail["viewer_user_id"], int)
    assert isinstance(detail["fact_version"], str)
    assert "sections" not in detail
    assert "支持" not in str(detail)

"""roR8Pk 第 1 条收口（口径：按**事项状态**延后公开）。

口径（owner 2026-09-16 定稿，覆盖 09-14 的「聚合计数 + k=3」方案）：
- 事项非终态时：`list_stances` 只返回**本人**的立场；他人立场不出现在列表中。
  ⚠️ **没有「按轮聚合计数」这个东西** —— 这条口径按事项状态判定，不按人数，
  也不提供任何形式的计数载荷。
- `get_stance` 对他人立场 404；终态（completed / cancelled）后全员可见。
- 发起人（仲裁者）不受限（PRD 第 3 章角色表）。
- 事项未终态时列表读审计照记（detail 只含计数与本人范围）。
"""

import pytest

from hub.api import matters as matter_svc
from hub.api import stances as stance_svc
from hub.db.models import Matter
from hub.domain.digest import compute_stance_content_hash
from hub.schemas.stance import StanceCreate
from tests.conftest import make_user


def _payload(*, round_number=1, stance="support", content_hash=None, **kw):
    fields = {
        "round_number": round_number, "stance": stance, "confidence": 0.6,
        "position_summary": "p", "rationale_summary": "r",
        "non_negotiables": [], "conditions": [], "open_questions": [],
        "depends_on": [], "questions_for": [], "disagreement_kind": None,
        "supersedes": None, "acting_as": "human", "authority": None,
        "ttl_seconds": None, "urgency": "normal", "visibility": "participants",
    }
    fields.update(kw)
    fields["content_hash"] = content_hash or compute_stance_content_hash(fields)
    return StanceCreate(**fields)


@pytest.fixture()
def three_person_matter(db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id, carol.id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    return {"matter": matter, "init": init, "alice": alice, "bob": bob,
            "carol": carol}


def _two_stances(db_session, matter, alice, bob):
    stance_svc.create_stance(db_session, matter_id=matter.id, user=alice,
                             payload=_payload(round_number=1, stance="support"))
    stance_svc.create_stance(db_session, matter_id=matter.id, user=bob,
                             payload=_payload(round_number=1, stance="oppose"))
    db_session.commit()


def test_未终态时成员列表只见本人加聚合计数(db_session, three_person_matter):
    m = three_person_matter
    _two_stances(db_session, m["matter"], m["alice"], m["bob"])
    rows = stance_svc.list_stances(
        db_session, matter_id=m["matter"].id, user=m["carol"])
    # 他人立场不出现在列表
    assert {s.user_id for s in rows} == set()


def test_未终态时他人立场单读返回404(db_session, three_person_matter):
    m = three_person_matter
    _two_stances(db_session, m["matter"], m["alice"], m["bob"])
    from hub.api.errors import ApiError

    with pytest.raises(ApiError) as exc:
        stance_svc.get_stance(db_session, matter_id=m["matter"].id,
                              user=m["carol"], target_user_id=m["alice"].id)
    assert exc.value.status_code == 404


def test_终态后全员可见(db_session, three_person_matter):
    m = three_person_matter
    _two_stances(db_session, m["matter"], m["alice"], m["bob"])
    matter = db_session.get(Matter, m["matter"].id)
    matter.status = "completed"
    db_session.commit()
    rows = stance_svc.list_stances(
        db_session, matter_id=m["matter"].id, user=m["carol"])
    assert {s.user_id for s in rows} == {m["alice"].id, m["bob"].id}
    stance = stance_svc.get_stance(
        db_session, matter_id=m["matter"].id, user=m["carol"],
        target_user_id=m["alice"].id)
    assert stance.user_id == m["alice"].id


def test_发起人未终态仍可见全部(db_session, three_person_matter):
    m = three_person_matter
    _two_stances(db_session, m["matter"], m["alice"], m["bob"])
    rows = stance_svc.list_stances(
        db_session, matter_id=m["matter"].id, user=m["init"])
    assert {s.user_id for s in rows} == {m["alice"].id, m["bob"].id}

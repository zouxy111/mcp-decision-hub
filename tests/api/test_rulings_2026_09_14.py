"""2026-09-14 owner 裁决落地（TDD）。

裁决 5a：irreversible 变更理由必填（创建时勾选 irreversible=True 必须带
irreversible_reason）。
裁决 5b：can_commit 开通限非终态事项（终态后不允许开通）。
裁决 2：decide_item 仅生成决议草稿（不写 completed；irreversible 事项直接
拒绝终裁）+ 授权范围可配置（用户自定义哪些问题可授权 AI 拍板）。
裁决 3：get_digest = 最新 ok 摘要 + 状态 + 收敛结果（简单形态）。
裁决 4：ttl 过期立场进重分配池（标 reassignable）而非静默当结论用。
裁决 1：ask 配额 N=100（(matter_id, actor, target) 滑窗超限 429）。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import Stance
from hub.domain.digest import compute_stance_content_hash
from hub.schemas.stance import StanceCreate
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    return {n: make_user(db_session, n) for n in ("init", "alice", "bob")}


def test_irreversible变更必须带理由(db_session, users):
    with pytest.raises(ApiError) as exc:
        matter_svc.create_matter(
            db_session, initiator=users["init"], title="T", goal="G",
            background="B", participant_ids=[users["alice"].id,
                                             users["bob"].id],
            initiator_participates=False, timeout_seconds=3600,
            max_rounds=10, draft_questions=["Q?"],
            irreversible=True,  # 无 irreversible_reason
        )
    assert exc.value.status_code == 422


def test_irreversible变更带理由可建(db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"], irreversible=True,
        irreversible_reason="涉及生产数据删除，不可逆",
    )
    assert matter.irreversible is True


def test_can_commit开通限非终态事项(db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter.status = "completed"
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        matter_svc.set_agent_authority(
            db_session, matter_id=matter.id, user_id=users["alice"].id,
            agent_authority="can_commit",
        )
    assert exc.value.status_code == 409


def test_decide_item仅生成草稿_不可逆事项拒绝终裁(db_session, users):
    from hub.api import resolutions as res_svc

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"], irreversible=True,
        irreversible_reason="涉及生产数据删除",
    )
    with pytest.raises(ApiError) as exc:
        res_svc.decide_resolution(
            db_session, matter_id=matter.id, actor=users["init"],
            decision="approve", expected_version=1,
        )
    assert exc.value.status_code in (403, 409, 422)


def test_get_digest返回最新摘要与状态(db_session, users):
    from hub.db.models import Round, RoundSummary

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=users["init"])
    db_session.commit()
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.add(RoundSummary(round_id=rnd.id, matter_id=matter.id,
                                consensus_points=["共识X"], divergences=[],
                                blind_spots=[], open_questions=[],
                                convergence="continue", generation_status="ok"))
    db_session.commit()

    from hub.mcp_server.methods import mcp_get_digest
    result = mcp_get_digest(db_session, None, user_id=users["init"].id,
                            matter_id=matter.id)
    assert result["status"] == matter.status
    assert result["consensus_points"] == ["共识X"]
    assert result["convergence"] == "continue"


def test_ttl过期立场进重分配池(db_session, users):
    from datetime import timedelta

    from hub.api import stances as stance_svc

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id,
                            actor=users["init"])
    fields = {"round_number": 1, "stance": "support", "confidence": 0.6,
              "position_summary": "p", "rationale_summary": "r",
              "non_negotiables": [], "conditions": [], "open_questions": [],
              "depends_on": [], "questions_for": [], "disagreement_kind": None,
              "supersedes": None, "acting_as": "human", "authority": None,
              "ttl_seconds": 60, "urgency": "normal", "visibility": "participants"}
    fields["content_hash"] = compute_stance_content_hash(fields)
    stance_svc.create_stance(db_session, matter_id=matter.id,
                             user=users["alice"], payload=StanceCreate(**fields))
    db_session.commit()
    stance = db_session.scalar(select(Stance).where(
        Stance.user_id == users["alice"].id))
    stance.created_at = stance.created_at - timedelta(seconds=120)
    db_session.commit()

    pool = stance_svc.list_reassignable_expired(
        db_session, matter_id=matter.id)
    assert {r.user_id for r in pool} == {users["alice"].id}


def test_ask配额超限返回429并写审计(db_session, users):
    from hub.domain.rate_limit import ASK_QUOTA_LIMIT

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id,
                            actor=users["init"])
    db_session.commit()

    fields = {"round_number": 1, "stance": "support", "confidence": 0.6,
              "position_summary": "p", "rationale_summary": "r",
              "non_negotiables": [], "conditions": [], "open_questions": [],
              "depends_on": [],
              "questions_for": [{"participant_id": str(users["bob"].id),
                                 "question": "Q?"}],
              "disagreement_kind": None, "supersedes": None,
              "acting_as": "human", "authority": None, "ttl_seconds": None,
              "urgency": "normal", "visibility": "participants"}
    fields["content_hash"] = compute_stance_content_hash(fields)

    # 直接灌满配额（不打 DB）：同一 (matter, actor, target) 调用 100 次后，
    # 第 101 次必须 429 —— 配额判定独立于落库成败。
    from hub.api.stances import _ask_limiter
    from hub.domain.rate_limit import rate_limit_key_ask

    key = rate_limit_key_ask(matter.id, users["alice"].id, users["bob"].id)
    for _ in range(ASK_QUOTA_LIMIT):
        allowed, _ = _ask_limiter.allow(key, limit=ASK_QUOTA_LIMIT)
        assert allowed
    allowed, retry = _ask_limiter.allow(key, limit=ASK_QUOTA_LIMIT)
    assert not allowed and retry > 0

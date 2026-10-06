"""r5Am9i · 立场层 MCP 工具（语义明确 4 个）：declare_item / submit_stance /
read_stance / get_summary。

- 输入经 hub/schemas 同源模型校验（rpQt6D：禁止裸 dict 出入参）；
- 输出经 hub/schemas/mcp_outputs 契约（D1）；
- 权限复用既有服务层闸门（非成员 404 语义不变）。
ask_participant / decide_item / get_digest 依赖未定产品口径（ask 配额 N、
can_commit 闸门、digest 语义——「方案」文档缺失），继续登记待口径。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import RoundSummary, Stance
from hub.domain.digest import compute_stance_content_hash
from hub.mcp_server.methods import (
    mcp_declare_item,
    mcp_get_summary,
    mcp_read_stance,
    mcp_submit_stance,
)
from hub.schemas.mcp_outputs import DeclareItemOut, RoundSummaryOut
from hub.schemas.stance import StanceRead
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    return {"alice": make_user(db_session, "alice"),
            "bob": make_user(db_session, "bob"),
            "carol": make_user(db_session, "carol")}


def _declare_payload(participant_ids):
    # 裁定 2（2026-09-17）：irreversible=true 必须带理由（此前校验被绕过，
    # 本 payload 恰好依赖那个漏洞行为）。
    return {"title": "是否上线新结算系统", "question": "本季度要不要切换？",
            "background": "B", "participant_ids": participant_ids,
            "irreversible": True, "irreversible_reason": "结算切换不可回滚",
            "options": ["A 方案", "B 方案"]}


def test_declare_item_创建留言板并落地新列(db_session, settings, users):
    pids = [users["alice"].id, users["bob"].id]
    result = mcp_declare_item(db_session, settings, user_id=users["carol"].id,
                              payload=_declare_payload(pids))
    db_session.commit()

    DeclareItemOut.model_validate(result)
    from hub.db.models import Matter, MatterParticipant
    matter = db_session.get(Matter, result["matter_id"])
    assert matter is not None
    # 2026-10-04 形态改造：agent 声明出来的也是留言板，建好即开放、没有轮次
    assert matter.status == "open"
    assert matter.mode == "board"
    assert result["status"] == "open"
    assert matter.irreversible is True
    assert matter.irreversible_reason == "结算切换不可回滚"
    assert matter.options == ["A 方案", "B 方案"]
    assert matter.item_version == 1
    rows = db_session.scalars(select(MatterParticipant).where(
        MatterParticipant.matter_id == matter.id)).all()
    # 发起人（carol）自己也在板上 —— 留言板没有「不自答的发起人」这一说
    assert {r.user_id for r in rows} == set(pids) | {users["carol"].id}


def test_declare_item_可以零参与人建板(db_session, settings, users):
    """留言板不再强制 2–5 人：先建板、再发邀请链接拉人是正常用法。"""
    result = mcp_declare_item(
        db_session, settings, user_id=users["carol"].id,
        payload={"title": "T", "question": "Q", "background": "",
                 "participant_ids": []},
    )
    db_session.commit()
    from hub.db.models import Matter, MatterParticipant
    matter = db_session.get(Matter, result["matter_id"])
    assert matter.status == "open"
    rows = db_session.scalars(select(MatterParticipant).where(
        MatterParticipant.matter_id == matter.id)).all()
    assert [r.user_id for r in rows] == [users["carol"].id]


def test_declare_item_参与人超过上限422(db_session, settings, users):
    """下限放开了，上限仍在（形状校验先拦下，无需真建 51 个账号）。"""
    with pytest.raises(ApiError) as exc:
        mcp_declare_item(db_session, settings, user_id=users["carol"].id,
                         payload={"title": "T", "question": "Q",
                                  "participant_ids": list(range(1, 52))})
    assert exc.value.status_code == 422


def test_submit_stance_合法提交走契约_非法枚举422(db_session, settings, users):
    alice, bob = users["alice"], users["bob"]
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="T", goal="G", background="B",
        participant_ids=[bob.id, users["carol"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q1?"])
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=alice)
    db_session.commit()

    fields = {"round_number": 1, "stance": "support", "confidence": 0.6,
              "position_summary": "p", "rationale_summary": "r",
              "non_negotiables": [], "conditions": [], "open_questions": [],
              "depends_on": [], "questions_for": [], "disagreement_kind": None,
              "supersedes": None, "acting_as": "human", "authority": None,
              "ttl_seconds": None, "urgency": "normal",
              "visibility": "participants"}
    fields["content_hash"] = compute_stance_content_hash(fields)
    result = mcp_submit_stance(db_session, settings, user_id=bob.id,
                               matter_id=matter.id, payload=dict(fields))
    db_session.commit()
    StanceRead.model_validate(result)
    assert result["stance"] == "support"
    assert db_session.scalar(
        select(Stance).where(Stance.matter_id == matter.id)) is not None

    bad = dict(fields, stance="banana",
               content_hash=compute_stance_content_hash(
                   dict(fields, stance="banana")))
    with pytest.raises(ApiError) as exc:
        mcp_submit_stance(db_session, settings, user_id=bob.id,
                          matter_id=matter.id, payload=bad)
    assert exc.value.status_code == 422


def test_read_stance_返回目标参与人最新立场_非成员404(db_session, settings, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="T", goal="G", background="B",
        participant_ids=[bob.id, carol.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"])
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=alice)
    db_session.commit()

    with pytest.raises(ApiError) as exc:
        mcp_read_stance(db_session, settings, user_id=carol.id,
                        matter_id=matter.id, target_user_id=bob.id)
    assert exc.value.status_code == 404  # 尚无立场也按 404 语义

    fields = {"round_number": 1, "stance": "oppose", "confidence": 0.6,
              "position_summary": "反对", "rationale_summary": "r",
              "non_negotiables": [], "conditions": [], "open_questions": [],
              "depends_on": [], "questions_for": [], "disagreement_kind": None,
              "supersedes": None, "acting_as": "human", "authority": None,
              "ttl_seconds": None, "urgency": "normal",
              "visibility": "participants"}
    fields["content_hash"] = compute_stance_content_hash(fields)
    mcp_submit_stance(db_session, settings, user_id=bob.id,
                      matter_id=matter.id, payload=dict(fields))
    db_session.commit()

    # 终态后全员可见（owner 2026-09-14 裁决 B 方案）
    matter.status = "completed"
    db_session.commit()
    result = mcp_read_stance(db_session, settings, user_id=carol.id,
                             matter_id=matter.id, target_user_id=bob.id)
    StanceRead.model_validate(result)
    assert result["stance"] == "oppose"
    assert result["user_id"] == bob.id


def test_get_summary_返回最新ok摘要_无摘要404(db_session, settings, users):
    alice, bob = users["alice"], users["bob"]
    matter = matter_svc.create_matter(
        db_session, initiator=alice, title="T", goal="G", background="B",
        participant_ids=[bob.id, users["carol"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q1?"])
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=alice)
    db_session.commit()

    with pytest.raises(ApiError) as exc:
        mcp_get_summary(db_session, settings, user_id=alice.id,
                        matter_id=matter.id)
    assert exc.value.status_code == 404

    from hub.db.models import Round
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.add(RoundSummary(
        round_id=rnd.id, matter_id=matter.id,
        consensus_points=["a"], divergences=[], blind_spots=[],
        open_questions=[], convergence="continue", generation_status="ok"))
    db_session.commit()

    result = mcp_get_summary(db_session, settings, user_id=alice.id,
                             matter_id=matter.id)
    RoundSummaryOut.model_validate(result)
    assert result["consensus_points"] == ["a"]

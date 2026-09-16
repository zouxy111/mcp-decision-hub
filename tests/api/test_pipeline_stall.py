"""rUdiTJ · PRD-08 片 1（**验收切片本身**）：
「一条测试证明『无新增信息时不进下一轮、直接判僵持』」。

配套对照与结构断言：
- 片 2：有新增信息 → 正常开下一轮（既有路径零变化）
- 验收第 2 条：拉人走既有流程，**不存在第二套换人逻辑**（结构断言，源码扫描）
- 验收第 3 条：退避复用 `hub/domain/retry.py`，**不存在新退避实现**
"""

from pathlib import Path

import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import (
    BLOCKED_REASON_STALLED,
    run_round_pipeline,
)
from hub.db.models import AuditEvent, Matter, Round, RoundSummary, Stance
from tests.conftest import make_user

HUB = Path(__file__).resolve().parents[2] / "hub"


def _stance(db_session, *, matter_id, round_number, user_id, stance="support",
            conditions=("甲",), open_questions=()):
    db_session.add(Stance(
        matter_id=matter_id, round_number=round_number, user_id=user_id,
        stance=stance, confidence=0.6, position_summary="立场",
        rationale_summary="理由", non_negotiables=[], conditions=list(conditions),
        open_questions=list(open_questions), depends_on=[], questions_for=[],
        disagreement_kind=None, supersedes=None, acting_as="human",
        authority=None, ttl_seconds=None, urgency="normal",
        visibility="participants", content_hash="a" * 64,
    ))


@pytest.fixture()
def two_rounds(db_session):
    """第 1、2 轮都已 closed，最新轮是第 2 轮，事项 in_progress。"""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    r1 = db_session.scalar(select(Round).where(Round.round_number == 1))
    db_session.add(Round(matter_id=matter.id, round_number=2, status="closed",
                         questions=[{"question_id": "q1", "content": "Q?"}]))
    db_session.execute(update(Round).where(Round.id == r1.id)
                       .values(status="closed"))
    db_session.execute(update(Matter).where(Matter.id == matter.id)
                       .values(status="in_progress"))
    db_session.commit()
    r2 = db_session.scalar(select(Round).where(Round.round_number == 2))
    return {"matter": matter, "init": init, "alice": alice, "bob": bob,
            "r1": r1, "r2": r2}


def _write_summary(db_session, scenario, convergence="continue"):
    db_session.add(RoundSummary(
        round_id=scenario["r2"].id, matter_id=scenario["matter"].id,
        consensus_points=["共识"], divergences=["分歧"], blind_spots=[],
        open_questions=["未决"], convergence=convergence,
        generation_status="ok",
    ))
    db_session.commit()


def _identical_stances(db_session, s):
    """两轮立场**逐字段一致** —— 正是验收片 1 的场景。"""
    for rnd_number in (1, 2):
        _stance(db_session, matter_id=s["matter"].id,
                round_number=rnd_number, user_id=s["alice"].id)
        _stance(db_session, matter_id=s["matter"].id,
                round_number=rnd_number, user_id=s["bob"].id,
                stance="oppose", conditions=("乙",))
    db_session.commit()


def test_无新增信息时不进下一轮直接判僵持(
    db_session, session_factory, settings, two_rounds, make_fake_llm
):
    """⭐ 事项原文的验收标准原文就是这一条。"""
    s = two_rounds
    _identical_stances(db_session, s)
    _write_summary(db_session, s)
    llm = make_fake_llm([{"questions": ["不该被调用"]}])

    run_round_pipeline(session_factory, settings, round_id=s["r2"].id, llm=llm)

    db_session.expire_all()
    matter = db_session.get(Matter, s["matter"].id)
    assert matter.status == "blocked"
    assert matter.blocked_reason == BLOCKED_REASON_STALLED
    # 关键：没有开第 3 轮，也没有为出题调用 LLM（不空转）
    assert db_session.scalar(
        select(Round).where(Round.round_number == 3)) is None
    assert llm.calls == []

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "matter_blocked")
    ).all()
    assert events and events[-1].detail["reason"] == BLOCKED_REASON_STALLED
    assert events[-1].detail["round_number"] == 2


def test_有新增信息则正常开下一轮(
    db_session, session_factory, settings, two_rounds, make_fake_llm
):
    """片 2 对照：只要有差异就照旧开下一轮（既有路径零变化）。"""
    s = two_rounds
    _identical_stances(db_session, s)
    # 第二轮 alice 的立场变了 → 有新增信息
    db_session.execute(
        update(Stance).where(Stance.round_number == 2,
                             Stance.user_id == s["alice"].id)
        .values(stance="conditional")
    )
    db_session.commit()
    _write_summary(db_session, s)
    llm = make_fake_llm([{"questions": ["追问一？"]}])

    run_round_pipeline(session_factory, settings, round_id=s["r2"].id, llm=llm)

    db_session.expire_all()
    assert db_session.get(Matter, s["matter"].id).status == "collecting"
    assert db_session.scalar(
        select(Round).where(Round.round_number == 3)) is not None
    assert len(llm.calls) == 1


def test_新增未决问题也算有新信息(
    db_session, session_factory, settings, two_rounds, make_fake_llm
):
    """片 2 的另一个方向：不是只有「立场变了」才算。"""
    s = two_rounds
    _identical_stances(db_session, s)
    db_session.execute(
        update(Stance).where(Stance.round_number == 2,
                             Stance.user_id == s["bob"].id)
        .values(open_questions=["预算口径还没定"])
    )
    db_session.commit()
    _write_summary(db_session, s)
    llm = make_fake_llm([{"questions": ["追问一？"]}])

    run_round_pipeline(session_factory, settings, round_id=s["r2"].id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Matter, s["matter"].id).status == "collecting"


def test_不存在第二套换人逻辑():
    """验收第 2 条：拉人只走既有流程。源码扫描，全仓只有一处换人入口。"""
    hits = [p.relative_to(HUB).as_posix() for p in HUB.rglob("*.py")
            if "def reassign_" in p.read_text(encoding="utf-8")]
    assert hits == ["api/reassignment.py"], (
        f"出现了第二处换人实现：{hits}（PRD-08 明写不要另起一套）"
    )


def test_不存在新退避实现():
    """验收第 3 条：退避复用既有实现，本块不新增。"""
    hits = [p.relative_to(HUB).as_posix() for p in HUB.rglob("*.py")
            if "def backoff" in p.read_text(encoding="utf-8")]
    assert hits == ["domain/retry.py"], f"出现了第二处退避实现：{hits}"

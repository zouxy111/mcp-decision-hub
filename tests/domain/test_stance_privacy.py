"""roR8Pk 配套用例：3 人 2 轮局，能否从「公开摘要视图」反推出谁改了立场。

⚠️ 本文件记录的是**当前真实行为**，不是期望行为。三个用例分别钉住公开面的
三个层次（均为代码里真实存在的构造，非凭空捏造）：

1. `test_public_view_does_not_allow_identifying_stance_changer`
   —— 成员可见的立场列表（stance_svc.list_stances，hub/api/stances.py:307-316）
   是当前代码对全体成员公开的真实视图。当前它逐人逐轮带 user_id 直给，
   因此可以唯一反推改动者 → **按设计为红**，证明缺口真实存在。修好读接口
   粒度（聚合 + k 阈值 / 延后公开）后本测试应转绿。
2. `test_round_summary_payload_carries_no_identity`
   —— LLM 轮次摘要的真实外发载荷（pipeline._summary_to_dict，
   hub/api/pipeline.py:150-157）只含五字段、无身份 → 绿，钉住「摘要层
   本身不带身份」这一已成立事实。
3. `test_analysis_view_scopes_identity_to_viewer`
   —— analyze_round 的 audience 视图（hub/domain/audience.py:61-97）只含
   观察者本人信息 → 绿，钉住「受控通道当前防住了他人身份」。
"""

from sqlalchemy import select

from hub.api import audit
from hub.api import matters as matter_svc
from hub.api import stances as stance_svc
from hub.api.pipeline import _summary_to_dict
from hub.db.models import AuditEvent, RoundSummary
from hub.domain.digest import compute_stance_content_hash
from hub.schemas.stance import StanceCreate
from tests.conftest import make_user


def _stance_payload(*, round_number: int, stance: str,
                    position_summary: str) -> StanceCreate:
    """按 B3 口径构造合法 StanceCreate（content_hash 服务端会重算比对）。"""
    fields = {
        "round_number": round_number,
        "stance": stance,
        "confidence": 0.9,
        "position_summary": position_summary,
        "rationale_summary": f"理由：{position_summary}",
        "non_negotiables": [],
        "conditions": [],
        "open_questions": [],
        "depends_on": [],
        "questions_for": [],
        "disagreement_kind": None,
        "supersedes": None,
        "acting_as": "human",
        "authority": None,
        "ttl_seconds": None,
        "urgency": "normal",
        "visibility": "participants",
    }
    fields["content_hash"] = compute_stance_content_hash(fields)
    return StanceCreate(**fields)


def _build_three_person_two_round_matter(db_session):
    """3 人（alice/bob/carol）2 轮局：
    round1：alice=oppose，bob=oppose，carol=support
    round2：alice=oppose，bob=support（唯一改动者），carol=support
    发起人 ivy 不参与，仅作事项成员。
    """
    ivy = make_user(db_session, "ivy")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")
    matter = matter_svc.create_matter(
        db_session,
        initiator=ivy,
        title="是否采用方案 X",
        goal="决定是否采用方案 X",
        background="",
        participant_ids=[alice.id, bob.id, carol.id],
        initiator_participates=False,
        timeout_seconds=3600,
        max_rounds=3,
        draft_questions=["你是否支持方案 X？"],
    )
    script = [
        (alice, 1, "oppose"), (bob, 1, "oppose"), (carol, 1, "support"),
        (alice, 2, "oppose"), (bob, 2, "support"), (carol, 2, "support"),
    ]
    for user, round_number, stance in script:
        stance_svc.create_stance(
            db_session, matter_id=matter.id, user=user,
            payload=_stance_payload(
                round_number=round_number, stance=stance,
                position_summary=f"{user.username} 第 {round_number} 轮的立场",
            ),
        )
    db_session.commit()
    return matter, ivy, alice, bob, carol


def _infer_changers_from_public_view(public_rows) -> set[int]:
    """纯差分反推：把公开视图按轮分组，比对同一人相邻两轮立场，集合即改动者。"""
    by_round: dict[int, dict[int, str]] = {}
    for row in public_rows:
        by_round.setdefault(row.round_number, {})[row.user_id] = row.stance
    rounds = sorted(by_round)
    changers: set[int] = set()
    for prev, cur in zip(rounds, rounds[1:]):
        for uid, stance in by_round[cur].items():
            if by_round[prev].get(uid) != stance:
                changers.add(uid)
    return changers


def test_public_view_does_not_allow_identifying_stance_changer(db_session):
    """隐私期望：观察者（成员 carol）仅从被允许公开的视图，不应能唯一确定
    谁在轮间改了立场。

    roR8Pk 第 1 条：owner 2026-09-14 裁决 B 方案（延后终态公开），
    list_stances 在非终态下只返回本人立场 → 公开视图无他人立场可比对，
    反推集合为空 → 断言转绿。原 xfail 登记摘除（见交付报告翻转台账）。"""
    matter, _ivy, _alice, bob, carol = _build_three_person_two_round_matter(
        db_session
    )

    public_rows = stance_svc.list_stances(
        db_session, matter_id=matter.id, user=carol
    )

    inferred = _infer_changers_from_public_view(public_rows)
    assert not len(inferred) == 1, (
        "隐私缺口（roR8Pk 第 1 条）：成员公开视图可唯一反推轮间改动者 "
        f"user_id={sorted(inferred)}（真实改动者 bob.id={bob.id}）"
    )


def test_批量读取立场会写读取审计(db_session):
    """roR8Pk 第 3 条收口：list_stances / analyze_round 补 STANCE_READ
    （detail 只记范围与行数，不记立场内容）。原红测试里的「现状零审计」
    断言随本收口翻转，见交付报告翻转台账。"""
    matter, _ivy, _alice, _bob, carol = _build_three_person_two_round_matter(
        db_session
    )

    stance_svc.list_stances(db_session, matter_id=matter.id, user=carol)
    stance_svc.analyze_round(db_session, matter_id=matter.id, round_number=2,
                             user=carol)

    reads = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit.STANCE_READ)
    ).all()
    scopes = {(r.detail or {}).get("scope") for r in reads}
    assert {"list", "analysis"} <= scopes
    assert all(r.actor_user_id == carol.id for r in reads)


def test_round_summary_payload_carries_no_identity(db_session):
    """LLM 轮次摘要的真实外发载荷（_summary_to_dict）只含五字段：无 user_id、
    无计数、无时间戳。单看这一层，「2 人 oppose → 1 人 oppose」的差分输入
    不存在 —— 本测试为绿，钉住已成立事实。"""
    summary = RoundSummary(
        round_id="rnd_test", matter_id="mat_test",
        consensus_points=["大家都同意要做"],
        divergences=["对实施节奏有分歧"],
        blind_spots=[], open_questions=[],
        convergence="continue",
    )
    payload = _summary_to_dict(summary)
    assert set(payload) == {
        "consensus_points", "divergences", "blind_spots",
        "open_questions", "convergence",
    }
    for value in payload.values():
        assert not isinstance(value, (int, float)) or value is None


def test_analysis_view_scopes_identity_to_viewer(db_session):
    """analyze_round 的 audience 视图：观察者 carol 只见自己的信息，
    sections 文本不含他人 user_id，且出口扫描零违规 —— 受控通道当前防住
    了他人身份（绿）。对照：裸立场 API（list/get）无此保护，见红测试。"""
    matter, _ivy, alice, bob, carol = _build_three_person_two_round_matter(
        db_session
    )
    analysis = stance_svc.analyze_round(
        db_session, matter_id=matter.id, round_number=2, user=carol
    )
    text = "\n".join(analysis.sections.values())
    assert str(alice.id) not in text
    assert str(bob.id) not in text
    assert analysis.violations == []


def test_过期立场不进收敛判定且写审计(db_session):
    """roR8Pk 第 5 条（owner 2026-09-14 批准 stance_expired）：ttl 过期的立场
    从 analyze_round 输入剔除并写审计；不重新拉人（该口径仍待 owner）。"""
    from datetime import timedelta as _td

    from hub.api import audit as audit_mod
    from hub.db.models import AuditEvent, Stance

    matter, _ivy, alice, bob, carol = _build_three_person_two_round_matter(
        db_session
    )
    # 把 alice 的 round2 立场改为「已过期」（created_at 回拨超过 ttl）
    expired = db_session.scalar(
        select(Stance).where(Stance.user_id == alice.id,
                             Stance.round_number == 2)
    )
    expired.ttl_seconds = 60
    expired.created_at = expired.created_at - _td(seconds=120)
    db_session.commit()

    analysis = stance_svc.analyze_round(
        db_session, matter_id=matter.id, round_number=2, user=carol
    )
    assert str(alice.id) not in "\n".join(analysis.sections.values())

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit_mod.STANCE_EXPIRED)
    ).all()
    assert len(events) == 1
    assert events[0].detail["user_id"] == alice.id
    assert events[0].detail["ttl_seconds"] == 60

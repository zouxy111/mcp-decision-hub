"""立场层跨线闭环：四层在同一份数据上串完。

链路：Stance 行（含 1 条脏数据 ``stance="banana"``）
-> ``to_stance_inputs``（装配，封死 str/int 接缝）
-> ``evaluate_convergence_lenient``（宽容收敛判定 + 降级）
-> 降级审计（``convergence_degraded``）
-> ``to_fact_base(display_names=...)``（差异化分发的事实基座）
-> ``build_audience_view`` -> ``scan_view``（出口扫描）。

三条线各自的单元测试证明不了这个闭环：只有脏数据从库里读出来、被降级、又
如实带进分发与出口扫描时，「降级不许冒充收敛」与「没发现问题 ≠ 没有问题」
这两条契约才真正成立。
"""

import re

from sqlalchemy import select, text

from hub.api import audit
from hub.api.stances import analyze_round, to_fact_base, to_stance_inputs
from hub.db.models import AuditEvent, Matter, MatterParticipant, Stance
from hub.domain.audience import SCAN_LIMITATIONS, build_audience_view, scan_view
from hub.domain.convergence_eval import (
    UNDETECTED_CONDITIONAL_CONFLICT,
    UNDETECTED_NON_NEGOTIABLES,
    evaluate_convergence_lenient,
)
from tests.conftest import make_user

# 独立于实现的等价判据：不 import 私有 _ID_SHAPED，避免「拿实现证明实现」。
_ID_SHAPED_PROBE = re.compile(
    r"(?:用户|用戶|使用者|user|uid|id)\s*[:#＝=号]?\s*\d+", re.IGNORECASE
)


def _stance_row(*, matter_id, user_id, stance, confidence=0.6,
                non_negotiables=None, conditions=None,
                position_summary="p") -> Stance:
    """内存中的 Stance 行，content_hash 无关（不经 HTTP，不触发服务端校验）。"""
    return Stance(
        matter_id=matter_id, user_id=user_id, round_number=1, stance=stance,
        confidence=confidence, position_summary=position_summary,
        rationale_summary="r", non_negotiables=list(non_negotiables or []),
        conditions=list(conditions or []), open_questions=[], depends_on=[],
        questions_for=[], disagreement_kind=None, supersedes=None,
        acting_as="human", authority=None, ttl_seconds=None, urgency="normal",
        visibility="participants", content_hash="c" * 64,
    )


def _make_matter(db_session, initiator, *, participants) -> Matter:
    matter = Matter(initiator_id=initiator.id, title="是否上线推荐系统 v2",
                    goal="就是否上线达成结论", background="背景",
                    initiator_participates=True)
    db_session.add(matter)
    db_session.flush()
    for participant in participants:
        db_session.add(MatterParticipant(matter_id=matter.id,
                                         user_id=participant.id))
    db_session.flush()
    return matter


def test_四层闭环从脏数据到出口扫描(db_session):
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")
    matter = _make_matter(db_session, alice, participants=(alice, bob, carol))

    # 脏数据写入：真实 schema 的 CHECK 约束会拦住 stance="banana"。这里用
    # SQLite 的 ignore_check_constraints 模拟「存量库/外部直写」入库的脏行——
    # 本仓库没有迁移工具，CHECK 只对新建库生效，老库的 stances 表没有该约束，
    # 历史脏行 / 外部写入 / 新增立场类型忘了同步都会产生未知 stance。所以这
    # 不是在测一个假想分支。
    db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    db_session.add_all([
        _stance_row(matter_id=matter.id, user_id=alice.id, stance="support"),
        # conditional 同时带不可谈判项与条件：触发 C1、C2 两个未检测项
        _stance_row(matter_id=matter.id, user_id=bob.id, stance="conditional",
                    non_negotiables=["必须有人对风险兜底"],
                    conditions=["若成本可控"]),
        _stance_row(matter_id=matter.id, user_id=carol.id, stance="banana"),
    ])
    db_session.commit()
    db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    rows = db_session.scalars(
        select(Stance).where(Stance.matter_id == matter.id)
    ).all()
    display_names = {user.id: user.username for user in (alice, bob, carol)}

    # —— 第 1/2 层：装配 + 宽容收敛判定 ——
    result = evaluate_convergence_lenient(to_stance_inputs(rows))
    # ① 脏数据被跳过即降级；降级绝不允许冒充收敛。
    assert result.degraded is True
    assert result.converged is False
    assert result.skipped_stance_user_ids == (str(carol.id),)
    # ⑤ C1/C2 未检测项如实登记，不默认「无冲突」。
    assert set(result.undetected_checks) >= {
        UNDETECTED_NON_NEGOTIABLES,
        UNDETECTED_CONDITIONAL_CONFLICT,
    }

    # —— 第 3/4 层：差异化分发 + 出口扫描 ——
    fact = to_fact_base(
        rows=rows, fact_version=f"{matter.id}:r1", item_title=matter.title,
        question=matter.goal, decision=None, rationale="",
        display_names=display_names,
    )
    view = build_audience_view(fact, alice.id)
    rendered = "\n".join(view.sections.values())
    # ③ 成品文本里不含任何 id 形态的 token（标题里的 "v2" 只是普通数字）。
    assert "v2" in rendered
    assert _ID_SHAPED_PROBE.search(rendered) is None
    scan = scan_view(fact, view)
    # ④「没发现问题」与「我能发现什么」必须分开表达，缺一不可。
    assert scan.violations == []
    assert scan.limitations == SCAN_LIMITATIONS

    # —— 生产装配入口：同一条链 + 落降级审计 ——
    analysis = analyze_round(db_session, matter_id=matter.id, round_number=1,
                             user=alice)
    assert analysis.degrading is True
    assert analysis.converged is False
    assert analysis.skipped_user_ids == (str(carol.id),)
    assert set(analysis.undetected_checks) >= {
        UNDETECTED_NON_NEGOTIABLES,
        UNDETECTED_CONDITIONAL_CONFLICT,
    }
    # 手工串的四层与生产入口给出完全一致的视图与扫描结论。
    assert analysis.sections == view.sections
    assert analysis.violations == scan.violations
    assert analysis.limitations == scan.limitations

    # ② 降级审计恰好一条，且 detail 里的 user_id 是 int（不是 str）。
    db_session.expire_all()
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == audit.CONVERGENCE_DEGRADED
        )
    ).all()
    assert len(events) == 1
    assert events[0].matter_id == matter.id
    assert events[0].detail["stance"] == "banana"
    assert isinstance(events[0].detail["user_id"], int)
    assert events[0].detail["user_id"] == carol.id

"""立场服务层。

与 hub/api/*.py 其余模块一致：零 FastAPI 依赖，签名 (session, *, ...)，
事务由调用方（路由）负责提交。

权限裁定（本模块是唯一事实源）：
- 「事项成员」= 发起人或参与人，复用 matters.get_matter_for_user，闸门在
  _require_matter_access —— 非成员一律 404，这条不可放宽；
- visibility=participants（默认）= 事项成员（发起人 ∪ 参与人）可见；
  visibility=all = 保留值，v1 与 participants 同义，留待未来跨事项/公开可见。
  可见性自此只作留痕/审计属性，读侧不再产生权限差异 —— 后人不要"修复"成
  再挡发起人；
- 提交立场 = 必须是该事项参与人（发起人但未参与也不行）；
- 一切不满足的情形统一 404 RESOURCE_NOT_FOUND，且文案与「事项不存在」
  一致，避免通过状态码/文案泄露「该事项存在」。

装配（A1）：Stance 行 -> StanceInput / FactBase 的转换是生产代码，不再是
测试专属。str/int 的类型转换**只允许**发生在 to_stance_inputs 内部：
convergence_eval 用 str 作 user_id，audience 用 int 作 user_id，接缝必须封死。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import Matter, Stance, User
from hub.domain.audience import (
    FactBase,
    build_audience_view,
    scan_view,
)
from hub.domain.convergence_eval import (
    StanceInput,
    evaluate_convergence_lenient,
)
from hub.domain.digest import compute_stance_content_hash
from hub.domain.timeutil import utcnow
from hub.schemas.stance import StanceCreate

NOT_FOUND_MESSAGE = "事项不存在"
STANCE_NOT_FOUND_MESSAGE = "立场不存在"

SUPPORT = "support"
OPPOSE = "oppose"


@dataclass(frozen=True)
class RoundStanceAnalysis:
    """一轮立场分析结果（只读聚合，不含 DB 句柄，可直接序列化）。"""

    fact_version: str
    sections: dict[str, str]
    violations: list[str]
    limitations: tuple[str, ...]
    converged: bool
    degrading: bool
    undetected_checks: tuple[str, ...]
    skipped_user_ids: tuple[str, ...]


def to_stance_inputs(rows: Sequence[Stance]) -> list[StanceInput]:
    """Stance 行 -> 收敛判定的输入。str(user_id) 转换封在本函数内。"""
    return [
        StanceInput(
            user_id=str(row.user_id),
            stance=row.stance,
            confidence=row.confidence,
            non_negotiables=tuple(row.non_negotiables or ()),
            conditions=tuple(row.conditions or ()),
            disagreement_kind=row.disagreement_kind,
        )
        for row in rows
    ]


def to_fact_base(
    *,
    rows: Sequence[Stance],
    fact_version: str,
    item_title: str,
    question: str,
    decision: str | None,
    rationale: str,
    risks: Sequence[str] = (),
    action_items: Mapping[int, list[str]] | None = None,
    dissenting: Mapping[int, str] | None = None,
    display_names: Mapping[int, str] | None = None,
) -> FactBase:
    """Stance 行 -> 差异化分发的事实基座（int user_id，与 audience 侧一致）。"""
    if action_items is None:
        action_items = {}
    if dissenting is None:
        dissenting = {
            row.user_id: row.position_summary
            for row in rows
            if row.stance == OPPOSE
        }
    if display_names is None:
        display_names = {}
    return FactBase(
        fact_version=fact_version,
        item_title=item_title,
        question=question,
        decision=decision,
        rationale=rationale,
        supporting_user_ids=[r.user_id for r in rows if r.stance == SUPPORT],
        opposing_user_ids=[r.user_id for r in rows if r.stance == OPPOSE],
        risks=list(risks),
        action_items=dict(action_items),
        dissenting=dict(dissenting),
        display_names=dict(display_names),
    )


def _display_names(session: Session, rows: Sequence[Stance]) -> dict[int, str]:
    """装配层负责反查 users.username；domain（audience）不碰数据库。"""
    user_ids = {row.user_id for row in rows}
    if not user_ids:
        return {}
    return dict(
        session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        ).all()
    )


def analyze_round(
    session: Session,
    *,
    matter_id: str,
    round_number: int,
    user: User,
) -> RoundStanceAnalysis:
    """唯一的生产装配入口（A1 + C3）。

    成员闸门 -> 取本轮立场 -> to_stance_inputs -> 宽容收敛判定（对被跳过的
    脏数据逐条写 CONVERGENCE_DEGRADED 审计）-> to_fact_base -> 按调用方视角
    组装视图 -> 出口扫描。纯函数域（audience / convergence_eval）此前零生产
    调用方，全部装配集中在这一点上。
    """
    matter = _require_matter_access(session, matter_id=matter_id, user=user)
    rows = session.scalars(
        select(Stance)
        .where(Stance.matter_id == matter_id,
               Stance.round_number == round_number)
        .order_by(Stance.created_at.asc())
    ).all()
    # roR8Pk 第 5 条（owner 2026-09-14 批准 stance_expired）：过期立场不进
    # 收敛判定输入，并逐条写审计；不重新拉人（「由谁重新拉人」仍待 owner 口径）。
    now = utcnow()
    fresh_rows, expired_rows = [], []
    for row in rows:
        if row.ttl_seconds is not None and row.created_at + timedelta(
                seconds=row.ttl_seconds) <= now:
            expired_rows.append(row)
        else:
            fresh_rows.append(row)
    for row in expired_rows:
        audit.record_audit(
            session, audit.STANCE_EXPIRED,
            actor_user_id=None, matter_id=matter_id,
            detail={
                "matter_id": matter_id, "round_number": round_number,
                "user_id": row.user_id, "ttl_seconds": row.ttl_seconds,
            },
        )
    rows = fresh_rows
    # 聚合读同样留痕（roR8Pk 第 3 条收口）：detail 只记范围与行数，
    # 不记任何立场内容。
    audit.record_audit(
        session, audit.STANCE_READ,
        actor_user_id=user.id, matter_id=matter_id,
        detail={"scope": "analysis", "round_number": round_number,
                "returned_rows": len(rows)},
    )

    result = evaluate_convergence_lenient(to_stance_inputs(rows))
    # 降级分支的可达性：迁移机制已存在（hub/db/migrations/，v1/v2 起），
    # CHECK 约束对新建库直接生效、存量库由迁移步骤重建表补齐。但迁移前
    # 已入库的历史脏行 / 外部直写 / 新增立场类型忘了同步，仍会产生未知
    # stance。所以下面这段不是死代码 —— 它是存量库与外部写入方的主要防线，
    # 不要删。
    skipped = set(result.skipped_stance_user_ids)
    for row in rows:
        if str(row.user_id) in skipped:
            audit.record_audit(session, audit.CONVERGENCE_DEGRADED,
                               matter_id=matter_id,
                               detail={"stance": row.stance,
                                       "user_id": row.user_id})

    fact = to_fact_base(
        rows=rows,
        fact_version=f"{matter.id}:r{round_number}",
        item_title=matter.title,
        question=matter.goal,
        decision=None,
        # 恒为 None 的 decision 意味着此刻根本没有「结论依据」可讲。
        # background 是事项背景，语义不同，不能拿来冒充 rationale。
        rationale="",
        display_names=_display_names(session, rows),
    )
    view = build_audience_view(fact, user.id)
    scan = scan_view(fact, view)
    check_faithfulness(view.sections, fact)

    # rsEXuh ⑥留痕（AUDIENCE_VIEW_DELIVERED，owner 2026-09-14 批准）：
    # 给某个 viewer 交付了哪个版本的事实视图 + 内容哈希；detail 不含立场正文。
    audit.record_audit(
        session, audit.AUDIENCE_VIEW_DELIVERED,
        actor_user_id=user.id, matter_id=matter_id,
        detail={
            "viewer_user_id": user.id,
            "fact_version": fact.fact_version,
            "content_hash": compute_stance_content_hash(
                {"sections": view.sections, "fact_version": fact.fact_version}
            ),
        },
    )

    return RoundStanceAnalysis(
        fact_version=fact.fact_version,
        sections=view.sections,
        violations=scan.violations,
        limitations=scan.limitations,
        converged=result.converged,
        degrading=result.degraded,
        undetected_checks=result.undetected_checks,
        skipped_user_ids=result.skipped_stance_user_ids,
    )


def _require_matter_access(
    session: Session, *, matter_id: str, user: User
) -> Matter:
    """读侧入口：事项成员（发起人或参与人）放行，其余一律 404。"""
    matter = matter_svc.get_matter_for_user(
        session, matter_id=matter_id, user=user
    )
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)
    return matter


def _is_participant(session: Session, *, matter_id: str, user_id: int) -> bool:
    return matter_svc.is_participant(session, matter_id=matter_id,
                                     user_id=user_id)


def _visible(
    session: Session, *, matter: Matter, user: User, stance: Stance
) -> bool:
    """可见性过滤（A2 + owner 2026-09-14 裁决：延后终态公开）。

    - 发起人（仲裁者，PRD 第 3 章角色表）恒可见；
    - 事项终态（completed / cancelled）后，成员全员可见；
    - 非终态时：成员只见本人立场（user_id 一致）——逐人立场不提前公开，
      消除 2–5 人规模下的跨轮反推（roR8Pk 第 1 条）。
    调用方须已通过 _require_matter_access（成员闸门在上一层拦过非成员）。
    """
    if matter.initiator_id == user.id:
        return True
    if matter.status in ("completed", "cancelled"):
        return True
    return stance.user_id == user.id


def create_stance(
    session: Session,
    *,
    matter_id: str,
    user: User,
    payload: StanceCreate,
) -> Stance:
    """把已通过 Schema 校验的载荷落为一条立场记录（同一轮同人唯一）。

    先校验事项存在且当前用户是参与人：否则 SQLite 外键会抛 IntegrityError
    变成 500，这里必须提前拦成 404。
    再按 B3 口径重算 content_hash 并比对 —— 摘要必须绑定正文，客户端传
    什么不再等于存什么。
    """
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)
    if not _is_participant(session, matter_id=matter_id, user_id=user.id):
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)

    # 09-11 owner 放开 can_commit 时同时加的两道锁之一
    # （r5Am9i 验收第 4 条 = rRSEcS 验收第 3 条）：**irreversible 事项一律
    # 强制拉人**，代理不得自行承诺不可逆决定。与「decide_resolution 不允许
    # Agent 终裁」（hub/api/resolutions.py:71-77）是同一口径的两侧落点 ——
    # 那边管终裁，这边管承诺。
    if matter.irreversible and payload.authority == "can_commit":
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED,
            actor_user_id=user.id, matter_id=matter_id,
            detail={"action": "submit_stance", "reason": "irreversible",
                    "authority": payload.authority,
                    "acting_as": payload.acting_as.value},
        )
        raise ApiError(403, "FORBIDDEN_DENIED",
                       "不可逆事项强制拉人：代理不得自行承诺不可逆决定")

    fields = payload.model_dump(mode="json")
    if payload.content_hash != compute_stance_content_hash(fields):
        raise ApiError(422, "VALIDATION_FAILED",
                       "content_hash 与提交正文不匹配（摘要不匹配）")

    stance = Stance(
        matter_id=matter_id,
        user_id=user.id,
        round_number=payload.round_number,
        stance=payload.stance.value,
        confidence=payload.confidence,
        position_summary=payload.position_summary,
        rationale_summary=payload.rationale_summary,
        non_negotiables=list(payload.non_negotiables),
        conditions=list(payload.conditions),
        open_questions=list(payload.open_questions),
        depends_on=list(payload.depends_on),
        questions_for=[q.model_dump() for q in payload.questions_for],
        disagreement_kind=(
            payload.disagreement_kind.value if payload.disagreement_kind else None
        ),
        supersedes=payload.supersedes,
        acting_as=payload.acting_as.value,
        authority=payload.authority,
        ttl_seconds=payload.ttl_seconds,
        urgency=payload.urgency.value,
        visibility=payload.visibility.value,
        content_hash=payload.content_hash,
    )
    session.add(stance)
    session.flush()
    return stance


def get_stance(
    session: Session,
    *,
    matter_id: str,
    user: User,
    target_user_id: int,
) -> Stance:
    """读取某位参与人在该事项上最新一轮的立场，并写审计。

    不存在的 user_id、跨事项的 user_id、以及可见性不足，一律 404。
    """
    matter = _require_matter_access(session, matter_id=matter_id, user=user)
    stance = session.scalar(
        select(Stance)
        .where(Stance.matter_id == matter_id, Stance.user_id == target_user_id)
        .order_by(Stance.round_number.desc())
    )
    if stance is None or not _visible(session, matter=matter, user=user,
                                      stance=stance):
        raise ApiError(404, "RESOURCE_NOT_FOUND", STANCE_NOT_FOUND_MESSAGE)
    audit.record_audit(
        session, audit.STANCE_READ,
        actor_user_id=user.id, matter_id=matter_id,
        detail={"target_user_id": target_user_id, "stance_id": stance.stance_id},
    )
    return stance


def list_stances(session: Session, *, matter_id: str, user: User) -> list[Stance]:
    """列出该事项下当前用户可见的全部立场（按轮次升序）。

    批量读是拉取他人立场的主入口，必须有读取侧审计（roR8Pk 第 3 条收口）。
    """
    matter = _require_matter_access(session, matter_id=matter_id, user=user)
    rows = session.scalars(
        select(Stance)
        .where(Stance.matter_id == matter_id)
        .order_by(Stance.round_number.asc(), Stance.created_at.asc())
    ).all()
    visible = [s for s in rows if _visible(session, matter=matter, user=user,
                                           stance=s)]
    audit.record_audit(
        session, audit.STANCE_READ,
        actor_user_id=user.id, matter_id=matter_id,
        detail={"scope": "list", "returned_rows": len(visible)},
    )
    return visible


# rsEXuh · faithfulness 完整校验（owner 2026-09-14 裁决归属 rsEXuh）。
# 对外消息中凡承载事实的 sections 字段，必须逐字来自 FactBase 的对应字段；
# 唯一允许的非逐字内容是固定占位文案白名单（结论未定/未记录反对意见/无
# 行动项/无风险这类空态句式）。校验失败则 raise（产出即拦下，不带病出门）。
_NO_DECISION_PLACEHOLDER = "这事目前还没定下来。"
_NO_RECORD_PLACEHOLDER = "这次没有记录下你当时的具体意见。"
_NO_ACTION_PLACEHOLDER = "这次没有要你做的事。"
_NO_RISK_PLACEHOLDER = "目前没有特别提到的风险。"

_FIXED_PLACEHOLDERS = frozenset({
    _NO_DECISION_PLACEHOLDER, _NO_RECORD_PLACEHOLDER,
    _NO_ACTION_PLACEHOLDER, _NO_RISK_PLACEHOLDER,
    "这事目前还没定下来，你的意见还在桌面上。",
})


def check_faithfulness(view_sections: dict[str, str], fact: FactBase) -> None:
    """逐字校验 view.sections 是否忠实于 fact。任一承载事实的字段不在
    {对应源字段, 白名单} 即抛 FaithfulnessError。"""
    # 决定事项 / 要解决的问题：逐字 = item_title / question
    if view_sections.get("决定事项") != fact.item_title:
        raise FaithfulnessError(
            f"决定事项不忠实: {view_sections.get('决定事项')!r}"
            f" != {fact.item_title!r}")
    if view_sections.get("要解决的问题") != fact.question:
        raise FaithfulnessError(
            f"要解决的问题不忠实: {view_sections.get('要解决的问题')!r}"
            f" != {fact.question!r}")

    # 结论：decision 存在时逐字；否则必须是固定占位文案
    if fact.decision:
        if view_sections.get("结论") != fact.decision:
            raise FaithfulnessError(
                "结论不忠实于 decision（决策存在但文本被改写）")
    else:
        if view_sections.get("结论") != _NO_DECISION_PLACEHOLDER:
            raise FaithfulnessError("无结论时结论段必须是固定占位文案，不得编结论")

    # 为什么这么定：decision 存在时逐字 = rationale；不存在时该段必须不出现
    if fact.decision:
        if view_sections.get("为什么这么定") != fact.rationale:
            raise FaithfulnessError("为什么这么定不忠实于 rationale")
    else:
        if "为什么这么定" in view_sections:
            raise FaithfulnessError("无结论时不得出现「为什么这么定」")

    # 风险：逐字（_bullets 输出源自 risks；空集 → 固定占位）
    expected_risks = (
        _NO_RISK_PLACEHOLDER if not fact.risks
        else "\n".join(f"- {r}" for r in fact.risks)
    )
    if view_sections.get("还要注意的风险") != expected_risks:
        raise FaithfulnessError("风险段不忠实于 risks")

    # 你当初的意见：只在 viewer 是反对者时出现，逐字 = dissenting[viewer]
    # 或固定占位；不得编造。viewer 身份不在 sections 里，所以从 dissenting
    # 反推（逐字忠实校验的对象本来就是内容，不是身份）。
    if "你当初的意见" in view_sections:
        candidates = {fact.dissenting.get(uid) or _NO_RECORD_PLACEHOLDER
                      for uid in fact.opposing_user_ids}
        if view_sections["你当初的意见"] not in candidates | {_NO_RECORD_PLACEHOLDER}:
            raise FaithfulnessError(
                "你当初的意见不忠实于 dissenting（不得替反对者编造异议）")

    # 为什么这次没有采纳 / 什么情况下会重新考虑：decision 存在时逐字由
    # rationale/risks 派生，但占位结论时不得出现
    if not fact.decision:
        for key in ("为什么这次没有采纳", "什么情况下会重新考虑"):
            if key in view_sections:
                raise FaithfulnessError(
                    f"无结论时不得出现「{key}」（没下结论却解释采纳理由）")


class FaithfulnessError(ValueError):
    """sections 承载事实的字段与 FactBase 不一致。"""

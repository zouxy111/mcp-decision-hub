"""收敛判定：分歧代价模型（纯函数，零 I/O）。

与 hub.domain.convergence 的区别：那个模块只做四态字符串合法性校验，本模块
在立场数据上做结构化收敛判定。判据落在硬条件上，agreement_score 仅作展示，
不单独作为判据。输入缺失（超时未表态）由调用方决定，本模块不猜、不补。
"""

from dataclasses import dataclass, field

STANCE_SUPPORT = "support"
STANCE_OPPOSE = "oppose"
STANCE_CONDITIONAL = "conditional"
STANCE_ABSTAIN = "abstain"
STANCE_NEED_INFO = "need_info"

DIVERGENCE_FACT = "fact"
DIVERGENCE_GOAL = "goal"
DIVERGENCE_RISK_APPETITE = "risk_appetite"
DIVERGENCE_RESOURCE = "resource"

# 明确反对的置信度门槛：达到即视为硬阻塞。
OPPOSE_BLOCK_CONFIDENCE = 0.7


class ConvergenceEvalError(ValueError):
    """立场数据非法（未知 stance 等）。"""


@dataclass(frozen=True)
class StanceInput:
    """一条立场表态。只读五个字段 + user_id，保持零依赖可独立开发。"""

    user_id: str
    stance: str
    confidence: float = 1.0
    non_negotiables: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    disagreement_kind: str | None = None


@dataclass(frozen=True)
class Divergence:
    user_ids: tuple[str, ...]
    kind: str
    description: str


@dataclass(frozen=True)
class Faction:
    user_ids: tuple[str, ...]
    stances: frozenset[str]


@dataclass(frozen=True)
class ConvergenceResult:
    converged: bool
    agreement_score: float
    divergences: list[Divergence] = field(default_factory=list)
    factions: list[Faction] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)


_STANCE_WEIGHTS = {
    STANCE_SUPPORT: 1.0,
    STANCE_CONDITIONAL: 0.6,
    STANCE_ABSTAIN: 0.5,
    STANCE_NEED_INFO: 0.3,
    STANCE_OPPOSE: 0.0,
}


def _agreement_score(stances: list[StanceInput]) -> float:
    if not stances:
        return 0.0
    return sum(_STANCE_WEIGHTS.get(s.stance, 0.0) for s in stances) / len(stances)


def _factions(stances: list[StanceInput]) -> list[Faction]:
    grouped: dict[str, list[str]] = {}
    for s in stances:
        grouped.setdefault(s.stance, []).append(s.user_id)
    return [
        Faction(user_ids=tuple(users), stances=frozenset({stance}))
        for stance, users in grouped.items()
    ]


def _dominant_kind(stances: list[StanceInput]) -> str:
    for s in stances:
        if s.disagreement_kind in (
            DIVERGENCE_FACT,
            DIVERGENCE_GOAL,
            DIVERGENCE_RISK_APPETITE,
            DIVERGENCE_RESOURCE,
        ):
            return s.disagreement_kind
    return DIVERGENCE_GOAL


def evaluate_convergence(stances: list[StanceInput]) -> ConvergenceResult:
    for s in stances:
        if s.stance not in _STANCE_WEIGHTS:
            raise ConvergenceEvalError(f"未知立场: {s.stance!r}")

    blocking: list[str] = []
    divergences: list[Divergence] = []

    opposers = [s for s in stances if s.stance == STANCE_OPPOSE]
    if opposers:
        names = "、".join(s.user_id for s in opposers)
        divergences.append(
            Divergence(
                user_ids=tuple(s.user_id for s in stances),
                kind=_dominant_kind(opposers),
                description=f"用户 {names} 持反对立场，与其余参与人存在直接对立",
            )
        )
        firm = [s for s in opposers if s.confidence >= OPPOSE_BLOCK_CONFIDENCE]
        if firm:
            firm_names = "、".join(s.user_id for s in firm)
            blocking.append(
                f"用户 {firm_names} 明确反对且置信度 "
                f">= {OPPOSE_BLOCK_CONFIDENCE}，结论无法收敛"
            )
        weak = [s for s in opposers if s.confidence < OPPOSE_BLOCK_CONFIDENCE]
        if weak:
            weak_names = "、".join(s.user_id for s in weak)
            blocking.append(
                f"用户 {weak_names} 持反对立场（置信度 < {OPPOSE_BLOCK_CONFIDENCE}），"
                "未达硬阻断门槛但仍不构成共识"
            )

    risk_appetite = [
        s for s in stances if s.disagreement_kind == DIVERGENCE_RISK_APPETITE
    ]
    if risk_appetite:
        risk_ids = {s.user_id for s in risk_appetite}
        names = "、".join(s.user_id for s in risk_appetite)
        others = [s.user_id for s in stances if s.user_id not in risk_ids]
        divergences.append(
            Divergence(
                user_ids=tuple(s.user_id for s in risk_appetite) + tuple(others),
                kind=DIVERGENCE_RISK_APPETITE,
                description=(
                    f"用户 {names} 与其余人在风险承受意愿上分歧："
                    "补信息无用，只能拉人（引入能对风险拍板的人）"
                ),
            )
        )
        blocking.append(f"风险偏好分歧（{names}）：补信息无用，只能拉人")

    # 硬条件 1：未接受结论的人所持的不可谈判项，视为与结论直接冲突。
    # 支持者默认其不可谈判项已被结论满足；未接受者无法确认，统统按未收敛处理。
    unmet = [
        s for s in stances if s.stance != STANCE_SUPPORT and s.non_negotiables
    ]
    for s in unmet:
        items = "、".join(s.non_negotiables)
        divergences.append(
            Divergence(
                user_ids=(s.user_id,),
                kind=_dominant_kind([s]),
                description=f"用户 {s.user_id} 的不可谈判项（{items}）与当前结论直接冲突",
            )
        )
        blocking.append(
            f"用户 {s.user_id} 的不可谈判项（{items}）与结论直接冲突，无法收敛"
        )

    # 硬条件 3：need_info 说明尚未表态；若分歧属事实类，则补信息有可能解决。
    for s in stances:
        if s.stance != STANCE_NEED_INFO:
            continue
        if s.disagreement_kind == DIVERGENCE_FACT:
            divergences.append(
                Divergence(
                    user_ids=(s.user_id,),
                    kind=DIVERGENCE_FACT,
                    description=f"用户 {s.user_id} 因事实信息不足无法表态：补信息能解决",
                )
            )
            blocking.append(f"事实信息缺口（{s.user_id}）：补信息能解决，收敛待补")
        else:
            divergences.append(
                Divergence(
                    user_ids=(s.user_id,),
                    kind=_dominant_kind([s]),
                    description=f"用户 {s.user_id} 尚未表态（need_info），需补齐前提",
                )
            )
            blocking.append(f"用户 {s.user_id} 尚未表态，无法收敛")

    settled = all(
        s.stance in (STANCE_SUPPORT, STANCE_CONDITIONAL, STANCE_ABSTAIN)
        for s in stances
    )
    converged = bool(stances) and settled and not blocking

    if not stances:
        blocking.append("无参与人立场，无法评估收敛")

    blocking += [
        f"用户 {s.user_id} 弃权：不阻断收敛，但绝不能静默当成同意"
        for s in stances
        if s.stance == STANCE_ABSTAIN
    ]

    return ConvergenceResult(
        converged=converged,
        agreement_score=_agreement_score(stances),
        divergences=divergences,
        factions=_factions(stances),
        blocking=blocking,
    )

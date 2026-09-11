"""差异化分发：把同一份事实基座，向不同接收者产出不同的消息。

纯函数，零 IO —— 不调 LLM、不碰数据库、不读文件。
裁剪由代码确定性执行，不依赖提示词（提示词可以被注入绕过，代码不会）。

管线顺序固定为四步：
  (1) 事实基座 FactBase（结构化，不是一段自然语言）
  (2) 按接收者确定性裁剪 —— 主防线，此时模型还没看到任何内容
  (3) 用裁剪后的子集组装消息  -> build_audience_view
  (4) 出口扫描 —— 兜底，走一遍禁止内容检查 -> scan_view
出口扫描扫的是 (3) 的产物，顺序不能颠倒。
"""

import re
from dataclasses import dataclass, field
from typing import Iterable

# 兜底扫描只认「id 形态」：用户 2 / 用户2 / user 2 / user_id=2 / uid:2 …
# 裸整数不判（业务正文里到处都是数字：v2、3 个方案、2 阶段）。
# 权限判定由结构化数据（visible_user_ids）负责，正则不再承担这个职责。
_ID_SHAPED = re.compile(
    r"(?:用户|用戶|使用者|user|uid|id)\s*[:#＝=号]?\s*(\d+)", re.IGNORECASE
)


@dataclass(frozen=True)
class FactBase:
    """结构化的事实基座，所有接收者的消息都只从这里裁剪而来。"""

    fact_version: str
    item_title: str
    question: str
    decision: str | None
    rationale: str
    supporting_user_ids: list[int]
    opposing_user_ids: list[int]
    risks: list[str]
    action_items: dict[int, list[str]]
    dissenting: dict[int, str]
    # 契约（2026-09-12 冻结）：user_id -> 显示名（users.username）。
    # 装配层负责填充；domain 不碰数据库。缺省空 dict，存量构造全部兼容。
    display_names: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AudienceView:
    """某一个接收者能看到的那一份。"""

    viewer_user_id: int
    sections: dict[str, str]
    visible_user_ids: list[int]
    fact_version: str


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def build_audience_view(fact: FactBase, viewer_user_id: int) -> AudienceView:
    """按接收者确定性裁剪，再组装成消息。"""
    sections: dict[str, str] = {
        "决定事项": fact.item_title,
        "要解决的问题": fact.question,
        "结论": fact.decision if fact.decision else "这事目前还没定下来。",
    }
    if fact.decision:
        # 结论还没定，就不能紧接着解释「为什么这么定」——那是自相矛盾。
        sections["为什么这么定"] = fact.rationale
    sections["还要注意的风险"] = _bullets(fact.risks, "目前没有特别提到的风险。")

    if viewer_user_id in fact.supporting_user_ids:
        sections["你的意见被采纳了"] = "你支持的方向，这次被采纳了。"

    if viewer_user_id in fact.opposing_user_ids:
        # 只逐字转述基座里记下的原话；没记下就如实说没记下，不替对方编一段。
        sections["你当初的意见"] = fact.dissenting.get(viewer_user_id) or (
            "这次没有记录下你当时的具体意见。"
        )
        if fact.decision:
            sections["为什么这次没有采纳"] = f"这次结论的依据是：{fact.rationale}"
            sections["什么情况下会重新考虑"] = _reconsider_condition(fact)
        else:
            # 结论还没定，就不能说「为什么没有采纳」——那是自相矛盾。
            sections["现在还没有结论"] = "这事目前还没定下来，你的意见还在桌面上。"

    sections["你要做的事"] = _bullets(
        fact.action_items.get(viewer_user_id, []), "这次没有要你做的事。"
    )

    return AudienceView(
        viewer_user_id=viewer_user_id,
        sections=sections,
        visible_user_ids=[viewer_user_id],
        fact_version=fact.fact_version,
    )


def _reconsider_condition(fact: FactBase) -> str:
    """重新考虑的条件只从事实基座里已有的内容（遗留风险）推出来，
    本函数不替反对者编造任何异议文字。"""
    if fact.risks:
        return "如果这些风险真的发生，或者出现了新的证据，这个结论会重新拿出来讨论。"
    return "如果出现了新的证据，这个结论会重新拿出来讨论。"


@dataclass(frozen=True)
class ScanResult:
    """出口扫描的结果。blocked 为真表示这份消息被拦下，不能发出去。

    limitations：本次扫描在结构上覆盖不到的范围，随结果一起返回。
    调用方须把它一并暴露出去，不得把「没发现问题」当成「没有问题」。
    """

    blocked: bool
    violations: list[str]
    limitations: tuple[str, ...] = ()


# 出口扫描做不到的事，固定三条，随 ScanResult 一起返回。
# 调用方必须把它一并暴露出去：扫描说「没发现问题」，不等于「没有问题」。
SCAN_LIMITATIONS: tuple[str, ...] = (
    "仅覆盖 build_audience_view 产出的 sections 文本，"
    "不覆盖调用方在 sections 之外附加的内容",
    "只识别 id 形态（如 user 2 / user_id=2），正文里的裸整数一律不判为泄漏",
    "不做语义判断，改写、拼音、指代形式的泄漏无法发现",
)


def participant_user_ids(fact: FactBase) -> frozenset[int]:
    """事实基座里出现过的所有用户 id —— 出口扫描用它判断某个数字是不是"人"。"""
    ids = set(fact.supporting_user_ids) | set(fact.opposing_user_ids)
    ids |= set(fact.action_items)
    ids |= set(fact.dissenting)
    return frozenset(ids)


def scan_view(
    fact: FactBase,
    view: AudienceView,
    *,
    forbidden_terms: Iterable[str] = (),
) -> ScanResult:
    """出口扫描：兜底的一道断言，扫的是已经组装好的那一份消息。

    这不是权限判定的主防线 —— 主防线是 build_audience_view 的确定性裁剪，
    权限由结构化数据（visible_user_ids）负责。这里只做「撞见 id 形态」的兜底，
    宁可少报也不要把正常业务文本里的数字误判成泄漏。

    什么算禁止内容，三类：
    1. 未授权的用户 id —— 文本里以 id 形态（如「用户 2」「user_id=2」）出现了
       事实基座中存在的用户编号，但不在这份视图的 visible_user_ids 里；
       裸整数一律不判；
    2. 未授权用户的异议原文 —— 别人的反对意见被原样搬了进来；
    3. 调用方给的敏感词 —— 由外部传入的 forbidden_terms。
    """
    violations: list[str] = []
    text = "\n".join(view.sections.values())
    visible = set(view.visible_user_ids)
    known = participant_user_ids(fact)

    reported: set[int] = set()
    for token in _ID_SHAPED.findall(text):
        number = int(token)
        if number in known and number not in visible and number not in reported:
            reported.add(number)
            # 文案里只说人话（显示名），不回显裸 id —— 违规提示本身不该再泄漏一次 id。
            violations.append(
                f"出现了未授权的用户：{fact.display_names.get(number) or '未命名成员'}"
            )

    for uid, words in fact.dissenting.items():
        if uid not in visible and words and words in text:
            violations.append(f"泄漏了未授权用户的意见原文（用户 {uid}）")

    for term in forbidden_terms:
        if term and term in text:
            violations.append(f"命中了禁止内容: {term}")

    return ScanResult(
        blocked=bool(violations),
        violations=violations,
        limitations=SCAN_LIMITATIONS,
    )

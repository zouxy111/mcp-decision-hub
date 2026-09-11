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
from dataclasses import dataclass
from typing import Iterable

_INT_TOKEN = re.compile(r"(?<!\d)\d+(?!\d)")


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
        "为什么这么定": fact.rationale,
        "还要注意的风险": _bullets(fact.risks, "目前没有特别提到的风险。"),
    }

    if viewer_user_id in fact.supporting_user_ids:
        sections["你的意见被采纳了"] = "你支持的方向，这次被采纳了。"

    if viewer_user_id in fact.opposing_user_ids:
        # 只逐字转述基座里记下的原话；没记下就如实说没记下，不替对方编一段。
        sections["你当初的意见"] = fact.dissenting.get(viewer_user_id) or (
            "这次没有记录下你当时的具体意见。"
        )
        sections["为什么这次没有采纳"] = f"这次结论的依据是：{fact.rationale}"
        sections["什么情况下会重新考虑"] = _reconsider_condition(fact)

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
    """出口扫描的结果。blocked 为真表示这份消息被拦下，不能发出去。"""

    blocked: bool
    violations: list[str]


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
    """出口扫描：兜底的一道检查，扫的是已经组装好的那一份消息。

    什么算禁止内容，三类：
    1. 未授权的用户 id —— 文本里出现了事实基座中存在的用户编号，
       但不在这份视图的 visible_user_ids 里；
    2. 未授权用户的异议原文 —— 别人的反对意见被原样搬了进来；
    3. 调用方给的敏感词 —— 由外部传入的 forbidden_terms。
    """
    violations: list[str] = []
    text = "\n".join(view.sections.values())
    visible = set(view.visible_user_ids)
    known = participant_user_ids(fact)

    reported: set[int] = set()
    for token in _INT_TOKEN.findall(text):
        number = int(token)
        if number in known and number not in visible and number not in reported:
            reported.add(number)
            violations.append(f"出现了未授权的用户 id: {number}")

    for uid, words in fact.dissenting.items():
        if uid not in visible and words and words in text:
            violations.append(f"泄漏了未授权用户的意见原文（用户 {uid}）")

    for term in forbidden_terms:
        if term and term in text:
            violations.append(f"命中了禁止内容: {term}")

    return ScanResult(blocked=bool(violations), violations=violations)

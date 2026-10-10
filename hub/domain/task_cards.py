"""任务卡纯函数（v22，2026-10-10）。

刻意不放的事（与 :mod:`hub.domain.todos` 同一纪律）：数据库 CRUD、
成员权限校验、LLM 审查 —— 那些分别在 ``hub/api/task_cards.py`` 与调用方。
本模块只放状态机、JSON 列的解析/序列化、自评与审查结果的归一化。

背景：owner 2026-10-10 裁定补「任务下发→开工→交付验收」这段协同质量
链路（测试反馈：杨琦 2026-10-08~09）。软门禁：AI 只指出缺陷，不阻断。
"""

import json

# draft（与发布人讨论验收标准中）→ published（已发布，约束协作人）
# → closed（关闭，不再接收交付）
TASK_CARD_STATUSES = ("draft", "published", "closed")
CARD_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"published", "closed"}),
    "published": frozenset({"closed"}),
    "closed": frozenset(),
}

DELIVERY_STATUSES = ("submitted", "accepted", "rejected")
DELIVERY_DECISIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"accepted", "rejected"}),
    "accepted": frozenset(),
    "rejected": frozenset(),
}

# AI 审查结论
REVIEW_VERDICTS = ("pass", "gap", "unclear")
REVIEW_STATUSES = ("pass", "gaps", "failed")

MAX_CRITERIA = 20
MAX_CRITERION_LEN = 200
MAX_TITLE_LEN = 255
MAX_DESCRIPTION_LEN = 4000
MAX_SUMMARY_LEN = 8000
MAX_SELF_CHECK_NOTE = 500


class TaskCardError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def assert_card_transition(current: str, target: str) -> None:
    if target not in CARD_TRANSITIONS.get(current, frozenset()):
        raise TaskCardError(f"任务卡状态 {current} 不允许变更为 {target}")


def assert_delivery_decision(current: str, target: str) -> None:
    if target not in DELIVERY_DECISIONS.get(current, frozenset()):
        raise TaskCardError(f"交付状态 {current} 不允许变更为 {target}")


def parse_criteria(raw: str) -> list[str]:
    """task_cards.acceptance_criteria（JSON 列）→ 字符串数组。"""
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(item) for item in value if isinstance(item, str)]


def dump_criteria(criteria: list[str]) -> str:
    return json.dumps(criteria, ensure_ascii=False)


def validate_criteria(criteria: list[str]) -> list[str]:
    """清洗发布人给的验收标准：去空白、去重、限条数限长度。

    空结果视为「还没讨论出标准」——发布前必须至少一条（见
    :func:`hub.api.task_cards.publish_card` 的校验）。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in criteria:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        cleaned.append(text[:MAX_CRITERION_LEN])
        seen.add(text)
        if len(cleaned) >= MAX_CRITERIA:
            break
    return cleaned


def normalize_self_check(items, criteria: list[str]) -> list[dict]:
    """把交付人的自评对齐到验收标准逐条。

    入参允许缺条/多条/顺序乱 —— 按 criteria 顺序重排，缺失的补
    ``{"met": None, "note": ""}``（未自评），未知条目丢弃。
    ``met`` 归一化为 True/False/None。
    """
    by_criterion: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("criterion") or "").strip()
        if not key:
            continue
        by_criterion[key] = item
    result: list[dict] = []
    for criterion in criteria:
        item = by_criterion.get(criterion, {})
        raw_met = item.get("met")
        met = raw_met if raw_met in (True, False) else None
        note = str(item.get("note") or "").strip()[:MAX_SELF_CHECK_NOTE]
        result.append({"criterion": criterion, "met": met, "note": note})
    return result


def parse_self_check(raw: str | None) -> list[dict]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict)]


def parse_ai_review(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def review_overall(verdicts: list[dict]) -> str:
    """从逐项结论归纳整单状态：任一 gap → gaps，全 pass → pass。

    审查本身抛错（LLM 不可用、JSON 不合契约）由调用方落成
    ``review_status="failed"``，不进这里。
    """
    if any(v.get("verdict") == "gap" for v in verdicts):
        return "gaps"
    return "pass"

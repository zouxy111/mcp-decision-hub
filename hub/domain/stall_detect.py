"""僵持判定（`rUdiTJ` · 开工包 PRD-08 片 1）。**纯函数域，零 IO。**

## 要解决的问题

收敛判定为 `continue` 时，平台**默认**会开下一轮。但如果本轮立场相对上一轮
**没有任何内容差异**，再开一轮就是空转 —— 白烧一次 LLM、白占一轮额度，
参与者还被要求重复表态。口径（事项原文逐字）：

> 不收敛 → 判「有新增信息吗」：**没有则判僵持、立即拉人，不空转**；
> 有则只回传差异点进下一轮

本模块只回答一个问题：**这两轮之间有没有新增信息。** 「拉人」不在本模块
（走 `hub/api/pipeline.py` 的既有路径 + `hub/api/reassignment.py`，不另起一套）。

## 「新增信息」的定义（信号清单）

五类信号，任一命中即判「有新增信息」：

| 信号 | 含义 |
|---|---|
| `NEW_STANCE` | 本轮出现了上一轮没有的参与人立场 |
| `STANCE_REMOVED` | 上一轮有、本轮没有的参与人立场 |
| `STANCE_CHANGED` | 同一参与人的立场取值变了（support/oppose/…） |
| `NEGOTIABLES_CHANGED` | 底线集合有增或删 |
| `CONDITIONS_CHANGED` | 条件集合有增或删 |
| `OPEN_QUESTIONS_CHANGED` | 待答问题集合有增或删 |
| `QUESTIONS_FOR_CHANGED` | 定向提问集合有增或删 |

⚠️ **刻意不计入**：`position_summary` / `rationale_summary` 的**措辞**变化，
以及 `confidence` 的**数值**变化。

理由：这两类只要模型重新生成一次就会变（同一个意思换个说法），把它们算成
「新信息」会让僵持判定**永不触发** —— 本功能就白做了。要收紧或放宽，改
`SIGNALS` 附近一处即可，不要散在各处判。

⚠️ **空数据不判僵持**：本轮**一条立场都没有**时一律返回「有新增信息」。
拿不到立场说不出「没有新信息」（更可能是数据缺失或任务全超时），
宁可多开一轮也不误拉人。验收切片（两轮都有立场且完全一致）不受影响。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

NEW_STANCE = "new_stance"
STANCE_REMOVED = "stance_removed"
STANCE_CHANGED = "stance_changed"
NEGOTIABLES_CHANGED = "non_negotiables_changed"
CONDITIONS_CHANGED = "conditions_changed"
OPEN_QUESTIONS_CHANGED = "open_questions_changed"
QUESTIONS_FOR_CHANGED = "questions_for_changed"

SIGNALS = (
    NEW_STANCE,
    STANCE_REMOVED,
    STANCE_CHANGED,
    NEGOTIABLES_CHANGED,
    CONDITIONS_CHANGED,
    OPEN_QUESTIONS_CHANGED,
    QUESTIONS_FOR_CHANGED,
)


@dataclass(frozen=True)
class StanceSnapshot:
    """一轮里某位参与人的立场快照 —— 与 ORM 解耦，便于纯函数测试。

    集合类字段在 `__post_init__` 里**统一归一**（去重）：顺序与重复不算差异
    —— 同一批条件换个次序不是「新信息」。归一化放在这里而不是 `from_row`，
    是为了让**任何构造路径**（含测试里手搓的快照）都过同一道归一，不会出现
    「从 ORM 来的归一了、手搓的没归一」这种半截口径。

    **必须 `sorted()`**：`set` 只保证「同一批元素去重」，**不保证迭代顺序** ——
    两个元素相同的 set 若插入顺序不同，迭代顺序可能不同，于是归一化结果
    不确定、比较结果随进程漂移（实测踩过：同提交同锁文件，我这边 714 全绿、
    干净环境 713 passed + 1 failed）。`sorted()` 是让归一化**确定**的那一步。
    """

    user_id: int
    stance: str
    non_negotiables: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    questions_for: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("non_negotiables", "conditions", "open_questions",
                      "questions_for"):
            object.__setattr__(self, field, _normalize(getattr(self, field)))

    @classmethod
    def from_row(cls, row) -> StanceSnapshot:
        """从 `hub.db.models.Stance` 行取快照。

        `questions_for` 归一成 `"target:question"` 字符串；缺字段的畸形项
        直接丢掉（列表里混进过非 `{participant_id, question}` 形状的东西）。
        """
        return cls(
            user_id=row.user_id,
            stance=row.stance,
            non_negotiables=tuple(row.non_negotiables or ()),
            conditions=tuple(row.conditions or ()),
            open_questions=tuple(row.open_questions or ()),
            questions_for=tuple(
                f"{q['participant_id']}:{q['question']}"
                for q in (row.questions_for or [])
                if isinstance(q, dict)
                and isinstance(q.get("participant_id"), str)
                and isinstance(q.get("question"), str)
            ),
        )


def _normalize(values: Iterable | None) -> tuple[str, ...]:
    return tuple(sorted({v for v in (values or []) if isinstance(v, str)}))


@dataclass(frozen=True)
class StallVerdict:
    """`stalled=True` 表示「无新增信息」（判僵持）；`signals` 列出命中的差异。"""

    stalled: bool
    signals: tuple[str, ...]

    @property
    def reason(self) -> str:
        """给审计/日志用的一句话。"""
        if self.stalled:
            return "本轮立场相对上一轮无任何内容差异"
        return "；".join(self.signals)


def snapshots_from_rows(rows: Sequence) -> dict[int, StanceSnapshot]:
    """按 `user_id` 归集；同一人一轮应当只有一行（表上有唯一约束兜底）。"""
    return {row.user_id: StanceSnapshot.from_row(row) for row in rows}


def detect_stall(
    previous: dict[int, StanceSnapshot],
    current: dict[int, StanceSnapshot],
) -> StallVerdict:
    """比较相邻两轮的立场快照，判定是否僵持。

    入参用 `dict[user_id, snapshot]` 而不是 `list`：比较是**按人**做的，
    用 list 会退化成顺序敏感的比较。
    """
    if not current:
        # 空数据不判僵持（见模块 docstring）。
        return StallVerdict(stalled=False, signals=(NEW_STANCE,))

    signals: list[str] = []
    if current.keys() - previous.keys():
        signals.append(NEW_STANCE)
    if previous.keys() - current.keys():
        signals.append(STANCE_REMOVED)

    for uid, now in current.items():
        before = previous.get(uid)
        if before is None:
            continue
        if before.stance != now.stance:
            signals.append(STANCE_CHANGED)
        if before.non_negotiables != now.non_negotiables:
            signals.append(NEGOTIABLES_CHANGED)
        if before.conditions != now.conditions:
            signals.append(CONDITIONS_CHANGED)
        if before.open_questions != now.open_questions:
            signals.append(OPEN_QUESTIONS_CHANGED)
        if before.questions_for != now.questions_for:
            signals.append(QUESTIONS_FOR_CHANGED)

    return StallVerdict(stalled=not signals, signals=tuple(signals))

"""待办事项的业务规则（v21，2026-10-07）。

这一层放两样东西：**状态机**（合法转移，与 :mod:`hub.domain.state` 同风格
fail closed）与**统计口径**（进度百分比、逾期判定）。

刻意不放的事：查询、权限校验、LLM 抽取。那些分别属于路由层、``participants``
的既有能力、和 :mod:`hub.domain.board`。

为什么统计口径要定在这里而不是让agent  自己算：agent 汇报时最常犯的错就是
「完成率 70%」这类口径分歧（分母含不含被dropped 的？逾期的算不算未完成？）。
口径必须只有一个事实源。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hub.domain.timeutil import utcnow


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------

TODO_STATUSES = ("open", "doing", "done", "dropped")

# 刻意宽松：待办不像决议那样有法律效力，回退（doing → open）很常见，
# 不值得为此报错。「完成 → 进行中」允许，因为 agent 汇报后可能发现没真做完。
TODO_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"doing", "done", "dropped"}),
    "doing": frozenset({"open", "done", "dropped"}),
    "done": frozenset({"open", "doing"}),
    "dropped": frozenset({"open"}),
}

# 「还没完」的口径：进度分母与「我该做什么」都只看这几态。
# dropped 不算未完成（是有意不做了，不是没做），否则完成率永远上不去。
ACTIVE_STATUSES = frozenset({"open", "doing"})


class TodoTransitionError(ValueError):
    """非法状态转移。"""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"非法待办状态转移: {current} -> {target}")


def assert_todo_transition(current: str, target: str) -> None:
    if target not in TODO_TRANSITIONS.get(current, frozenset()):
        raise TodoTransitionError(current, target)


# --------------------------------------------------------------------------
# 来源与确认
# --------------------------------------------------------------------------

TODO_SOURCES = (
    "manual",                    # 人手动建
    "extracted_from_message",    # AI 从留言抽取
    "from_resolution",           # 从决议拆分
)


def is_official(todo) -> bool:
    """这条待办是否算数。

    ``needs_confirm=True`` 的（AI 抽的）不算数：既不计入进度分子也不计入
    分母 —— 计入分母会让完成率凭空变低，计入分子则会把噪声当成果。
    """
    return not bool(todo.needs_confirm)


# --------------------------------------------------------------------------
# 逾期判定
# --------------------------------------------------------------------------

# 宽限：过了截止时间一整天才算逾期。深夜 23:59 的任务标成「逾期」是噪音，
# 会让人对真正的逾期麻木。
GRACE_PERIOD = timedelta(days=1)


def is_overdue(todo, *, now: datetime | None = None) -> bool:
    """是否逾期。

    只有**未完成**的才谈逾期：done / dropped 一律不算。AI 待确认的不算 ——
    它还没被认可成真任务。
    """
    if not is_official(todo):
        return False
    if todo.status not in ACTIVE_STATUSES:
        return False
    if todo.due_at is None:
        return False
    moment = now or utcnow()
    if todo.due_at.tzinfo is None:
        moment = moment.replace(tzinfo=None) if moment.tzinfo else moment
    return moment > todo.due_at + GRACE_PERIOD


def days_left(todo, *, now: datetime | None = None) -> int | None:
    """距截止还有几天。负数 = 已过期。无截止日返回 None。

    agent 汇报时最需要这个字段（「还有 3 天到期」），但让它自己算容易
    搞错时区和取整方向，所以服务端算好。
    """
    if todo.due_at is None:
        return None
    moment = now or utcnow()
    if todo.due_at.tzinfo is None and moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    delta = todo.due_at - moment
    return delta.days


# --------------------------------------------------------------------------
# 进度统计
# --------------------------------------------------------------------------

def completion_rate(todo_list) -> tuple[int, int, int]:
    """返回 ``(已完成数, 未完成数, 完成百分比)``。

    口径（agent 汇报「这个项目到哪了」时按这个算）：

    * 分母只数**正式待办**（``needs_confirm=False``）且状态不是 ``dropped`` 的；
    * ``done`` 进分子；
    * ``open`` / ``doing`` 进未完成；
    * AI 待确认的**两边都不进**；
    * 分母为 0 时完成率定义为 0（不返回 100，避免「空板子显示 100% 完成」）。

    分母为 0 返回 ``(0, 0, 0)``。
    """
    official = [t for t in todo_list
                if is_official(t) and t.status != "dropped"]
    total = len(official)
    if total == 0:
        return 0, 0, 0
    done = sum(1 for t in official if t.status == "done")
    pending = total - done
    return done, pending, round(done * 100 / total)
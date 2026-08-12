"""State machine transition matrices (PRD 7.1.1 / 7.3 / 7.4). Illegal transitions fail closed."""

MATTER_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"in_progress", "cancelled"}),
    "in_progress": frozenset({"collecting", "awaiting_decision", "blocked", "cancelled"}),
    "collecting": frozenset({"in_progress", "collecting", "cancelled"}),
    "awaiting_decision": frozenset({"in_progress", "completed", "cancelled"}),
    "blocked": frozenset({"in_progress", "collecting", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"submitted", "timeout", "reassigned", "cancelled"}),
    "submitted": frozenset(),
    "timeout": frozenset({"reassigned", "cancelled"}),
    "reassigned": frozenset(),
    "cancelled": frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"非法状态转移: {current} -> {target}")


def _assert(matrix: dict[str, frozenset[str]], current: str, target: str) -> None:
    if target not in matrix.get(current, frozenset()):
        raise InvalidTransitionError(current, target)


def assert_matter_transition(current: str, target: str) -> None:
    _assert(MATTER_TRANSITIONS, current, target)


def assert_task_transition(current: str, target: str) -> None:
    _assert(TASK_TRANSITIONS, current, target)


RESOLUTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_review"}),
    "pending_review": frozenset({"approved", "modified", "rejected"}),
    "approved": frozenset(),
    "modified": frozenset(),
    "rejected": frozenset(),
}


def assert_resolution_transition(current: str, target: str) -> None:
    _assert(RESOLUTION_TRANSITIONS, current, target)

"""Round collection completeness rules (PRD 6.3.1). Pure rules, no I/O.

A round is collected when every task is terminal for collection purposes.
M2 has no reassignment: 'reassigned' counts as terminal without tracking
replacement tasks.
"""

COLLECTED_TASK_STATUSES = frozenset(
    {"submitted", "timeout", "reassigned", "cancelled"}
)


def is_round_collected(task_statuses: list[str]) -> bool:
    """True iff every task of the round reached a terminal-for-collection
    status. An empty round (should not exist) is not collected."""
    if not task_statuses:
        return False
    return all(s in COLLECTED_TASK_STATUSES for s in task_statuses)


def count_submitted(task_statuses: list[str]) -> int:
    """Number of submitted tasks; 0 means 'no effective output' (PRD 6.3.1)."""
    return sum(1 for s in task_statuses if s == "submitted")

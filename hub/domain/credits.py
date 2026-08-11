"""Round credit rules (PRD 7.5). Pure rules, no I/O.

The round limit constrains AUTO-advance only. Each manual continue by the
initiator grants exactly one extra round.
"""

CREDIT_GRANT_PER_CONTINUE = 1


def auto_round_limit(max_rounds: int, granted_extra_rounds: int) -> int:
    return max_rounds + granted_extra_rounds


def can_auto_advance(
    *, current_round_number: int, max_rounds: int, granted_extra_rounds: int
) -> bool:
    """Whether the platform may auto-generate the round after
    current_round_number."""
    return current_round_number + 1 <= auto_round_limit(
        max_rounds, granted_extra_rounds
    )

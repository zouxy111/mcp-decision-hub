"""Idempotency decision table, PRD 9.4. Pure function, no I/O."""

from enum import Enum

TERMINAL_NON_SUBMITTED = frozenset({"timeout", "reassigned", "cancelled"})


class IdempotencyDecision(Enum):
    REPLAY_SAME_KEY = "replay_same_key"        # 200, return stored first response
    CONFLICT_SAME_KEY = "conflict_same_key"    # 409 IDEMPOTENCY_CONFLICT
    REPLAY_EQUIVALENT = "replay_equivalent"    # 200 + duplicate-submit audit
    ALREADY_SUBMITTED = "already_submitted"    # 409 TASK_ALREADY_SUBMITTED
    CREATE_NEW = "create_new"                  # normal path
    INVALID_STATE = "invalid_state"            # 409 INVALID_STATE_TRANSITION


def decide_idempotency(
    *,
    task_status: str,
    key_seen: bool,
    key_body_matches: bool | None = None,
    content_matches: bool | None = None,
) -> IdempotencyDecision:
    if key_seen:
        if key_body_matches:
            return IdempotencyDecision.REPLAY_SAME_KEY
        return IdempotencyDecision.CONFLICT_SAME_KEY
    if task_status in TERMINAL_NON_SUBMITTED:
        return IdempotencyDecision.INVALID_STATE
    if task_status == "submitted":
        if content_matches:
            return IdempotencyDecision.REPLAY_EQUIVALENT
        return IdempotencyDecision.ALREADY_SUBMITTED
    return IdempotencyDecision.CREATE_NEW

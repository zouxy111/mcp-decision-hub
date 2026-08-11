import pytest

from hub.domain.idempotency import IdempotencyDecision, decide_idempotency


@pytest.mark.parametrize(
    ("task_status", "key_seen", "key_body_matches", "content_matches", "expected"),
    [
        # row 1: same key, same body, submitted -> replay first response
        ("submitted", True, True, True, IdempotencyDecision.REPLAY_SAME_KEY),
        # row 2: same key, different body, any status -> conflict
        ("submitted", True, False, True, IdempotencyDecision.CONFLICT_SAME_KEY),
        ("pending", True, False, None, IdempotencyDecision.CONFLICT_SAME_KEY),
        # row 3: new key, identical content, submitted -> 200 replay + audit
        ("submitted", False, None, True, IdempotencyDecision.REPLAY_EQUIVALENT),
        # row 4: new key, different content, submitted -> 409 already submitted
        ("submitted", False, None, False, IdempotencyDecision.ALREADY_SUBMITTED),
        # row 5: new key, pending -> create
        ("pending", False, None, None, IdempotencyDecision.CREATE_NEW),
        # row 6: terminal non-submitted states -> 409 invalid state
        ("timeout", False, None, None, IdempotencyDecision.INVALID_STATE),
        ("reassigned", False, None, None, IdempotencyDecision.INVALID_STATE),
        ("cancelled", False, None, None, IdempotencyDecision.INVALID_STATE),
        ("timeout", True, True, None, IdempotencyDecision.REPLAY_SAME_KEY),
    ],
)
def test_decision_table(task_status, key_seen, key_body_matches, content_matches, expected):
    assert (
        decide_idempotency(
            task_status=task_status,
            key_seen=key_seen,
            key_body_matches=key_body_matches,
            content_matches=content_matches,
        )
        == expected
    )

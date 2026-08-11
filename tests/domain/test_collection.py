from hub.domain.collection import (
    COLLECTED_TASK_STATUSES,
    count_submitted,
    is_round_collected,
)


def test_all_submitted_is_collected():
    assert is_round_collected(["submitted", "submitted"]) is True


def test_pending_blocks_collection():
    assert is_round_collected(["submitted", "pending"]) is False


def test_timeout_counts_towards_collection():
    assert is_round_collected(["submitted", "timeout"]) is True


def test_reassigned_is_terminal_in_m2():
    assert "reassigned" in COLLECTED_TASK_STATUSES
    assert is_round_collected(["reassigned", "submitted"]) is True


def test_cancelled_counts_towards_collection():
    assert is_round_collected(["cancelled", "timeout"]) is True


def test_all_timeout_collected_but_zero_submitted():
    statuses = ["timeout", "timeout"]
    assert is_round_collected(statuses) is True
    assert count_submitted(statuses) == 0


def test_empty_round_is_not_collected():
    assert is_round_collected([]) is False


def test_count_submitted():
    assert count_submitted(["submitted", "timeout", "submitted"]) == 2

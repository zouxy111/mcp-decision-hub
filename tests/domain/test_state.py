import pytest

from hub.domain.state import (
    InvalidTransitionError,
    assert_matter_transition,
    assert_task_transition,
)


def test_matter_m1_happy_path():
    assert_matter_transition("draft", "in_progress")
    assert_matter_transition("in_progress", "collecting")


def test_matter_draft_to_collecting_illegal():
    # PRD 7.1: draft -> collecting is NOT a legal transition
    with pytest.raises(InvalidTransitionError) as exc:
        assert_matter_transition("draft", "collecting")
    assert exc.value.current == "draft"
    assert exc.value.target == "collecting"


def test_matter_terminal_states_have_no_exit():
    for target in ("draft", "in_progress", "collecting", "blocked"):
        with pytest.raises(InvalidTransitionError):
            assert_matter_transition("completed", target)
        with pytest.raises(InvalidTransitionError):
            assert_matter_transition("cancelled", target)


def test_matter_unknown_state_illegal():
    with pytest.raises(InvalidTransitionError):
        assert_matter_transition("bogus", "collecting")


def test_task_pending_to_submitted():
    assert_task_transition("pending", "submitted")
    assert_task_transition("pending", "timeout")


def test_task_terminal_states_have_no_exit():
    for current in ("submitted", "reassigned", "cancelled"):
        with pytest.raises(InvalidTransitionError):
            assert_task_transition(current, "submitted")


def test_task_timeout_to_submitted_illegal():
    # PRD 7.3: submitting to a timeout task must fail closed
    with pytest.raises(InvalidTransitionError):
        assert_task_transition("timeout", "submitted")

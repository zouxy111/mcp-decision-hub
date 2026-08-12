import pytest

from hub.domain.resolution import (
    DECISION_APPROVE,
    DECISION_MODIFIED,
    DECISION_REJECT,
    RESOLUTION_STATUS_PENDING_REVIEW,
    RESOLUTION_TERMINAL_STATUSES,
    ResolutionValidationError,
    compose_draft_text,
    validate_decision_payload,
)
from hub.domain.state import InvalidTransitionError, assert_resolution_transition


def test_resolution_matrix_happy_paths():
    assert_resolution_transition("draft", "pending_review")
    for target in ("approved", "modified", "rejected"):
        assert_resolution_transition("pending_review", target)


@pytest.mark.parametrize("terminal", sorted(RESOLUTION_TERMINAL_STATUSES))
def test_resolution_terminal_states_have_no_exit(terminal):
    with pytest.raises(InvalidTransitionError):
        assert_resolution_transition(terminal, "pending_review")


def test_resolution_cannot_go_backwards():
    with pytest.raises(InvalidTransitionError):
        assert_resolution_transition("pending_review", "draft")
    with pytest.raises(InvalidTransitionError):
        assert_resolution_transition("approved", "modified")


def test_approve_requires_nothing():
    validate_decision_payload(
        decision=DECISION_APPROVE, final_text=None, rationale=None
    )


def test_reject_requires_rationale():
    with pytest.raises(ResolutionValidationError):
        validate_decision_payload(
            decision=DECISION_REJECT, final_text=None, rationale=None
        )
    with pytest.raises(ResolutionValidationError):
        validate_decision_payload(
            decision=DECISION_REJECT, final_text=None, rationale="   "
        )
    validate_decision_payload(
        decision=DECISION_REJECT, final_text=None, rationale="证据不足"
    )


def test_modified_requires_final_text_and_rationale():
    with pytest.raises(ResolutionValidationError):
        validate_decision_payload(
            decision=DECISION_MODIFIED, final_text=None, rationale="理由"
        )
    with pytest.raises(ResolutionValidationError):
        validate_decision_payload(
            decision=DECISION_MODIFIED, final_text="最终文本", rationale=None
        )
    validate_decision_payload(
        decision=DECISION_MODIFIED, final_text="最终文本", rationale="理由"
    )


def test_unknown_decision_rejected():
    with pytest.raises(ResolutionValidationError):
        validate_decision_payload(
            decision="maybe", final_text=None, rationale=None
        )


def test_compose_draft_text_contains_four_blocks():
    text = compose_draft_text(
        recommendation="采用方案 A",
        rationale="两轮讨论后分歧已收敛",
        risks=["进度风险"],
        divergences=["成本口径"],
    )
    assert "采用方案 A" in text
    assert "两轮讨论后分歧已收敛" in text
    assert "进度风险" in text
    assert "成本口径" in text
    assert text.index("采用方案 A") < text.index("两轮讨论后分歧已收敛")


def test_pending_review_constant_matches_prd():
    assert RESOLUTION_STATUS_PENDING_REVIEW == "pending_review"

import pytest

from hub.domain.convergence import (
    CONVERGENCE_STATES,
    ConvergenceValidationError,
    validate_convergence,
)


def test_four_states_exact_set():
    assert CONVERGENCE_STATES == frozenset(
        {"continue", "provisionally_ready", "converged", "blocked"}
    )


@pytest.mark.parametrize(
    "value", ["continue", "provisionally_ready", "converged", "blocked"]
)
def test_valid_states_returned_verbatim(value):
    assert validate_convergence(value) == value


@pytest.mark.parametrize(
    "value",
    ["CONTINUE", " done", "ready", "", "converge", None, 42, ["continue"]],
)
def test_illegal_values_rejected(value):
    with pytest.raises(ConvergenceValidationError):
        validate_convergence(value)

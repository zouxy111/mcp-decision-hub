"""Convergence four-state validation (PRD 7.6). Pure rules, no I/O.

An illegal value from the LLM is treated as an LLM failure (retryable) by the
client layer — see hub.llm.client.
"""

CONVERGENCE_CONTINUE = "continue"
CONVERGENCE_PROVISIONALLY_READY = "provisionally_ready"
CONVERGENCE_CONVERGED = "converged"
CONVERGENCE_BLOCKED = "blocked"

CONVERGENCE_STATES = frozenset(
    {
        CONVERGENCE_CONTINUE,
        CONVERGENCE_PROVISIONALLY_READY,
        CONVERGENCE_CONVERGED,
        CONVERGENCE_BLOCKED,
    }
)


class ConvergenceValidationError(ValueError):
    """The LLM returned an illegal convergence value."""


def validate_convergence(value) -> str:
    if not isinstance(value, str) or value not in CONVERGENCE_STATES:
        raise ConvergenceValidationError(f"非法收敛状态: {value!r}")
    return value

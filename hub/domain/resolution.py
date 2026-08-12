"""Resolution state constants, decision payload validation and draft text
composition (PRD 7.4 / FR-21). Pure rules, no I/O."""

RESOLUTION_STATUS_DRAFT = "draft"
RESOLUTION_STATUS_PENDING_REVIEW = "pending_review"
RESOLUTION_STATUS_APPROVED = "approved"
RESOLUTION_STATUS_MODIFIED = "modified"
RESOLUTION_STATUS_REJECTED = "rejected"

RESOLUTION_TERMINAL_STATUSES = frozenset(
    {
        RESOLUTION_STATUS_APPROVED,
        RESOLUTION_STATUS_MODIFIED,
        RESOLUTION_STATUS_REJECTED,
    }
)

# Decision names ARE the target status names (PRD 7.4).
DECISION_APPROVE = RESOLUTION_STATUS_APPROVED
DECISION_MODIFIED = RESOLUTION_STATUS_MODIFIED
DECISION_REJECT = RESOLUTION_STATUS_REJECTED
DECISIONS = frozenset({DECISION_APPROVE, DECISION_MODIFIED, DECISION_REJECT})


class ResolutionValidationError(ValueError):
    """Decision payload violates FR-21 field requirements."""


def validate_decision_payload(
    *, decision: str, final_text: str | None, rationale: str | None
) -> None:
    """FR-21: reject requires a rationale; modified requires both final text
    and rationale; approve requires nothing."""
    if decision not in DECISIONS:
        raise ResolutionValidationError(f"未知拍板动作: {decision}")
    if decision == DECISION_REJECT and not (rationale or "").strip():
        raise ResolutionValidationError("驳回必须填写理由")
    if decision == DECISION_MODIFIED:
        if not (final_text or "").strip():
            raise ResolutionValidationError("修改通过必须填写最终文本")
        if not (rationale or "").strip():
            raise ResolutionValidationError("修改通过必须填写理由")


def compose_draft_text(
    *,
    recommendation: str,
    rationale: str,
    risks: list,
    divergences: list,
) -> str:
    """Final text for an approved resolution: the platform draft itself
    (PRD 7.4)."""
    lines = ["【建议】", recommendation, "", "【依据】", rationale, "", "【风险】"]
    lines += [f"- {item}" for item in risks] or ["- （无）"]
    lines += ["", "【分歧】"]
    lines += [f"- {item}" for item in divergences] or ["- （无）"]
    return "\n".join(lines)

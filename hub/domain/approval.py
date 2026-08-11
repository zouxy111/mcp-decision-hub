"""Human-approval trail window check (PRD 9.3). All datetimes are naive UTC."""

from datetime import datetime, timedelta

APPROVAL_MAX_AGE = timedelta(hours=24)


class ApprovalWindowError(ValueError):
    """approved_at is outside the allowed window."""


def validate_approved_at(approved_at: datetime, received_at: datetime) -> None:
    if approved_at > received_at:
        raise ApprovalWindowError("approved_at 晚于服务端接收时间")
    if approved_at < received_at - APPROVAL_MAX_AGE:
        raise ApprovalWindowError("approved_at 早于服务端接收时间超过 24 小时")

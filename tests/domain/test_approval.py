from datetime import timedelta

import pytest

from hub.domain.approval import ApprovalWindowError, validate_approved_at
from hub.domain.timeutil import utcnow


def test_equal_to_received_is_valid():
    now = utcnow()
    validate_approved_at(now, now)  # must not raise


def test_future_approved_at_rejected():
    now = utcnow()
    with pytest.raises(ApprovalWindowError, match="晚于"):
        validate_approved_at(now + timedelta(seconds=1), now)


def test_exactly_24h_ago_is_valid():
    now = utcnow()
    validate_approved_at(now - timedelta(hours=24), now)  # boundary: allowed


def test_older_than_24h_rejected():
    now = utcnow()
    with pytest.raises(ApprovalWindowError, match="24"):
        validate_approved_at(now - timedelta(hours=24, seconds=1), now)


def test_recent_past_is_valid():
    now = utcnow()
    validate_approved_at(now - timedelta(minutes=3), now)

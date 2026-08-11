from datetime import datetime, timezone

import pytest

from hub.domain.timeutil import iso_z, parse_iso_z, utcnow


def test_utcnow_returns_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - now
    assert abs(delta.total_seconds()) < 5


def test_iso_z_formats_naive_as_utc():
    dt = datetime(2026, 8, 11, 4, 5, 0)
    assert iso_z(dt) == "2026-08-11T04:05:00Z"


def test_iso_z_converts_aware_to_utc():
    dt = datetime(2026, 8, 11, 12, 5, 0, tzinfo=timezone.utc)
    assert iso_z(dt) == "2026-08-11T12:05:00Z"


def test_parse_iso_z_roundtrip():
    dt = parse_iso_z("2026-08-11T04:05:00Z")
    assert dt == datetime(2026, 8, 11, 4, 5, 0)
    assert dt.tzinfo is None


def test_parse_iso_z_rejects_garbage():
    with pytest.raises(ValueError):
        parse_iso_z("not-a-time")

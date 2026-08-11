"""Time helpers. All DB timestamps are naive UTC; the wire format is ISO 8601 with Z."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current time as naive UTC (the canonical storage form)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_z(dt: datetime) -> str:
    """Serialize to ISO 8601 UTC with Z suffix, second precision. Naive is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_z(s: str) -> datetime:
    """Parse an ISO 8601 timestamp into naive UTC. Raises ValueError on garbage."""
    parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)

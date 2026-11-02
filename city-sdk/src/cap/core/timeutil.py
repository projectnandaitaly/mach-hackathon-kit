"""RFC 3339 timestamps with millisecond precision in UTC."""

from __future__ import annotations

from datetime import UTC, datetime

from cap.core.errors import CapError


def format_time(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise CapError("SCHEMA_INVALID", "timestamp must be timezone-aware")
    utc = moment.astimezone(UTC)
    millis = utc.microsecond // 1000
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise CapError("SCHEMA_INVALID", "timestamp must be a string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CapError("SCHEMA_INVALID", "timestamp is not RFC 3339") from exc
    if parsed.tzinfo is None:
        raise CapError("SCHEMA_INVALID", "timestamp must be timezone-aware")
    return parsed.astimezone(UTC)

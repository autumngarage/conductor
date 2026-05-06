"""Shared relative-time parsing for CLI filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def since_cutoff(since: str | timedelta | None) -> datetime | None:
    if since is None:
        return None
    if isinstance(since, timedelta):
        return datetime.now(UTC) - since
    unit = since[-1:]
    try:
        amount = int(since[:-1])
    except ValueError as e:
        raise ValueError("since must be like 1h, 24h, or 7d") from e
    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    elif unit == "m":
        delta = timedelta(minutes=amount)
    else:
        raise ValueError("since must use m, h, or d")
    return datetime.now(UTC) - delta


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

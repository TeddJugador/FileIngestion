from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_ms(moment: datetime) -> str:
    """ISO 8601 UTC with milliseconds and a ``Z`` suffix: ``2026-11-03T09:14:22.512Z``."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

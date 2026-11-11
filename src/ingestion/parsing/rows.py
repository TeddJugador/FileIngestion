from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_VALUE_CHARS = 100


@dataclass(frozen=True)
class RowError:
    """One validation error. ``value`` may hold personal data: summary file only,
    never logs, metrics or events."""

    line: int
    field: str
    error_code: str
    message: str
    value: str

    def to_report(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "field": self.field,
            "errorCode": self.error_code,
            "message": self.message,
            "value": self.value,
        }


@dataclass(frozen=True)
class RawRow:
    """A record as read from the file, before schema validation.

    ``line``: CSV physical line where the record starts (header is line 1); JSON
    1-based array index; JSONL physical line.
    """

    line: int
    data: Any
    errors: tuple[RowError, ...] = field(default=())


def truncate_value(value: object) -> str:
    text = "" if value is None else value if isinstance(value, str) else str(value)
    return text[:MAX_VALUE_CHARS]

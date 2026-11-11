"""Reason codes and the exception that turns a bad file into a business outcome."""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    """``ImportFailed.reasonCode`` values (EVENT_CATALOGUE.md)."""

    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    HEADER_MISMATCH = "HEADER_MISMATCH"
    MISSING_METADATA = "MISSING_METADATA"
    UNREADABLE = "UNREADABLE"
    NO_VALID_ROWS = "NO_VALID_ROWS"
    MOVED_TO_DLQ = "MOVED_TO_DLQ"  # reserved for phase 2


class FileRejected(Exception):
    """A file-level rejection: publish ``ImportFailed`` and report the message as succeeded.

    ``detail`` must never contain file content (it may be logged); describe the problem,
    don't quote it. Library error messages (ijson, csv, json) can embed raw input, so
    callers translate them into a fixed description rather than passing them through.
    """

    def __init__(self, reason: ReasonCode, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

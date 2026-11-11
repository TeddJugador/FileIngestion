"""Error collection and the per-import ``summary.json``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ingestion.clock import iso_ms
from ingestion.parsing.rows import RowError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

MAX_DETAILED_ERRORS = 1_000


class ErrorCollector:
    """Keeps the first 1,000 errors in detail and counts all of them.

    Error ``value``s can contain personal data: they go into the summary file only.
    """

    def __init__(self, max_details: int = MAX_DETAILED_ERRORS) -> None:
        self._max = max_details
        self._details: list[RowError] = []
        self.total = 0

    def add(self, errors: Iterable[RowError]) -> None:
        for error in errors:
            self.total += 1
            if len(self._details) < self._max:
                self._details.append(error)

    @property
    def truncated(self) -> bool:
        return self.total > len(self._details)

    def codes(self) -> list[str]:
        """Distinct error codes, safe to log (no values)."""
        return sorted({e.error_code for e in self._details})

    def to_report(self) -> list[dict[str, Any]]:
        return [e.to_report() for e in self._details]


@dataclass
class Summary:
    import_id: str
    source_file_key: str
    source_version_id: str
    dataset_type: str
    school_id: str
    status: str
    started_at: datetime
    finished_at: datetime
    reason_code: str | None = None
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    errors: ErrorCollector = field(default_factory=ErrorCollector)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "importId": self.import_id,
            "sourceFileKey": self.source_file_key,
            "sourceVersionId": self.source_version_id,
            "datasetType": self.dataset_type,
            "schoolId": self.school_id,
            "status": self.status,
        }
        if self.reason_code is not None:
            body["reasonCode"] = self.reason_code
        body.update(
            {
                "totalRows": self.total_rows,
                "validRows": self.valid_rows,
                "invalidRows": self.invalid_rows,
                "errorsTruncated": self.errors.truncated,
                "errors": self.errors.to_report(),
                "startedAt": iso_ms(self.started_at),
                "finishedAt": iso_ms(self.finished_at),
                "durationMs": int((self.finished_at - self.started_at).total_seconds() * 1000),
            }
        )
        body.update(self.extra)
        return body


def write_summary(s3: S3Client, bucket: str, key: str, summary: Summary) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(summary.to_json(), ensure_ascii=False, indent=2).encode(),
        ContentType="application/json",
    )

"""A file just under the 20 MB tabular limit stays far below the 1024 MB Lambda.

Uses stub clients rather than moto: moto stores everything in this process, which
would make tracemalloc measure moto instead of the pipeline.
"""

from __future__ import annotations

import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aws_lambda_powertools import Logger

from ingestion.datasets import DEFAULT_MAX_TABULAR_BYTES
from ingestion.notifications import S3ObjectRef
from ingestion.pipeline import Pipeline, Services

HEADER = b"student_number,assessment_code,term_code,mark,max_mark\n"
LAMBDA_MEMORY_BYTES = 1024 * 1024 * 1024


def _rows() -> Iterator[bytes]:
    i = 0
    while True:
        student, assessment = i % 100_000, i // 100_000
        yield f"STU-2026-{student:05d},ASSESSMENT-{assessment:03d},2026-T4,{i % 51},50\n".encode()
        i += 1


def _synthetic_csv(limit: int) -> tuple[int, int]:
    """(size in bytes, row count) of the largest file under ``limit``."""
    size = len(HEADER)
    for rows, row in enumerate(_rows()):
        if size + len(row) > limit:
            return size, rows
        size += len(row)
    raise AssertionError("unreachable")


class _Body:
    """Generates the file lazily, chunk by chunk, like a StreamingBody."""

    def __init__(self, limit: int) -> None:
        self._limit = limit

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        buffer, size = bytearray(HEADER), len(HEADER)
        for row in _rows():
            if size + len(row) > self._limit:
                break
            buffer += row
            size += len(row)
            if len(buffer) >= chunk_size:
                yield bytes(buffer)
                buffer.clear()
        if buffer:
            yield bytes(buffer)

    def close(self) -> None:
        pass


class _StubS3:
    def __init__(self, limit: int, size: int) -> None:
        self._limit, self._size = limit, size
        self.uploaded_bytes = 0

    def head_object(self, **_: Any) -> dict[str, Any]:
        return {"ContentLength": self._size, "Metadata": {"uploaded-by": "USR-1"}}

    def get_object(self, **_: Any) -> dict[str, Any]:
        return {"Body": _Body(self._limit)}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.uploaded_bytes += len(kwargs["Body"])
        return {}

    def create_multipart_upload(self, **_: Any) -> dict[str, Any]:
        return {"UploadId": "u"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.uploaded_bytes += len(kwargs["Body"])
        return {"ETag": "e"}

    def complete_multipart_upload(self, **_: Any) -> dict[str, Any]:
        return {}

    def abort_multipart_upload(self, **_: Any) -> dict[str, Any]:
        return {}


class _StubDynamo:
    items = 0

    def batch_write_item(self, RequestItems: dict[str, list[Any]]) -> dict[str, Any]:
        (requests,) = RequestItems.values()
        self.items += len(requests)
        return {}


class _StubPublisher:
    def publish(self, *_: Any, **__: Any) -> str:
        return "01J0000000000000000000000"


class _NullMetrics:
    def record(self, *_: Any) -> None:
        pass


@pytest.mark.slow
def test_file_just_under_20mb_uses_a_fraction_of_lambda_memory() -> None:
    limit = DEFAULT_MAX_TABULAR_BYTES
    size, rows = _synthetic_csv(limit)
    assert limit - 100 < size <= limit

    s3, dynamo = _StubS3(limit, size), _StubDynamo()
    pipeline = Pipeline(
        Services(
            s3=s3,  # type: ignore[arg-type]
            dynamodb=dynamo,  # type: ignore[arg-type]
            publisher=_StubPublisher(),  # type: ignore[arg-type]
            metrics=_NullMetrics(),
            logger=Logger(service="memory-test"),
            records_table="T",
        )
    )
    ref = S3ObjectRef("b", "raw/marks/SCH-001/big.csv", "v1", size, "e", datetime.now(UTC))

    tracemalloc.start()
    try:
        result = pipeline.process(ref)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.status == "COMPLETED"
    assert result.valid_rows == rows
    assert dynamo.items == rows
    print(f"\n{rows:,} rows, {size / 1e6:.1f} MB file, peak traced memory {peak / 1e6:.1f} MB")
    assert peak < LAMBDA_MEMORY_BYTES * 0.15  # well under 1024 MB

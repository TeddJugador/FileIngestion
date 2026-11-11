from __future__ import annotations

from typing import Any

import pytest

from ingestion.storage import (
    MIN_PART_SIZE,
    BatchWriter,
    JsonlMultipartWriter,
    UnprocessedItemsError,
)
from tests.unit.aws import BUCKET, Harness

KEY = "processed/marks/SCH-001/imp-x/valid.jsonl"


class _SpyS3:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []
        self.part_sizes: list[int] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        def call(**kwargs: Any) -> Any:
            self.calls.append(name)
            if name == "upload_part":
                self.part_sizes.append(len(kwargs["Body"]))
            return method(**kwargs)

        return call


# --- multipart JSONL writer --------------------------------------------------------------


def test_small_output_uses_a_single_put_object(harness: Harness) -> None:
    s3 = _SpyS3(harness.s3)
    with JsonlMultipartWriter(s3, BUCKET, KEY) as writer:  # type: ignore[arg-type]
        writer.write_line(b'{"a":1}')
        writer.write_line(b'{"a":2}')
        assert writer.close() is True
    assert s3.calls == ["put_object"]
    assert harness.s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read() == b'{"a":1}\n{"a":2}\n'


def test_nothing_written_creates_no_object(harness: Harness) -> None:
    s3 = _SpyS3(harness.s3)
    with JsonlMultipartWriter(s3, BUCKET, KEY) as writer:  # type: ignore[arg-type]
        assert writer.close() is False
    assert s3.calls == []
    assert not harness.exists(KEY)


@pytest.mark.slow
def test_large_output_uses_multipart_with_full_size_parts(harness: Harness) -> None:
    s3 = _SpyS3(harness.s3)
    line = b"x" * 999  # 1000 bytes with the newline
    count = 12_000  # ~12 MB -> 2 full parts + a remainder
    with JsonlMultipartWriter(s3, BUCKET, KEY) as writer:  # type: ignore[arg-type]
        for _ in range(count):
            writer.write_line(line)
        writer.close()
    assert s3.calls[0] == "create_multipart_upload"
    assert s3.calls[-1] == "complete_multipart_upload"
    assert "put_object" not in s3.calls
    assert len(s3.part_sizes) == 3
    assert all(size >= MIN_PART_SIZE for size in s3.part_sizes[:-1])
    assert sum(s3.part_sizes) == count * 1000
    head = harness.s3.head_object(Bucket=BUCKET, Key=KEY)
    assert head["ContentLength"] == count * 1000
    assert harness.open_multipart_uploads() == []


@pytest.mark.slow
def test_exception_after_a_part_aborts_the_upload(harness: Harness) -> None:
    s3 = _SpyS3(harness.s3)

    def write_then_fail() -> None:
        with JsonlMultipartWriter(s3, BUCKET, KEY) as writer:  # type: ignore[arg-type]
            for _ in range(6_000):  # ~6 MB: one part uploaded
                writer.write_line(b"y" * 999)
            assert writer.part_count == 1
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()
    assert s3.calls[-1] == "abort_multipart_upload"
    assert harness.open_multipart_uploads() == []
    assert not harness.exists(KEY)


def test_exception_before_any_part_writes_nothing(harness: Harness) -> None:
    s3 = _SpyS3(harness.s3)

    def write_then_fail() -> None:
        with JsonlMultipartWriter(s3, BUCKET, KEY) as writer:  # type: ignore[arg-type]
            writer.write_line(b"z")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()
    assert s3.calls == []


def test_part_size_below_s3_minimum_is_refused(harness: Harness) -> None:
    with pytest.raises(ValueError, match="5 MiB"):
        JsonlMultipartWriter(harness.s3, BUCKET, KEY, part_size=1024)


# --- batch writer ------------------------------------------------------------------------


class _StubDynamo:
    """Leaves the first ``unprocessed_rounds`` responses partially unprocessed."""

    def __init__(self, unprocessed_rounds: int, *, always: bool = False) -> None:
        self.unprocessed_rounds = unprocessed_rounds
        self.always = always
        self.requests: list[int] = []

    def batch_write_item(self, RequestItems: dict[str, list[Any]]) -> dict[str, Any]:
        (requests,) = RequestItems.values()
        self.requests.append(len(requests))
        if self.always or len(self.requests) <= self.unprocessed_rounds:
            return {"UnprocessedItems": {"T": requests[:1]}}
        return {"UnprocessedItems": {}}


def _item(i: int) -> dict[str, Any]:
    return {"PK": {"S": f"k{i}"}, "SK": {"S": "s"}}


def test_flushes_in_batches_of_25() -> None:
    stub = _StubDynamo(0)
    writer = BatchWriter(stub, "T", sleep=lambda _: None)  # type: ignore[arg-type]
    for i in range(60):
        writer.put(_item(i))
    writer.flush()
    assert stub.requests == [25, 25, 10]
    assert writer.items_written == 60


def test_unprocessed_items_are_retried_with_backoff() -> None:
    sleeps: list[float] = []
    stub = _StubDynamo(unprocessed_rounds=2)
    writer = BatchWriter(stub, "T", sleep=sleeps.append, jitter=lambda: 1.0)  # type: ignore[arg-type]
    for i in range(25):
        writer.put(_item(i))
    assert stub.requests == [25, 1, 1]  # only the unprocessed item is resent
    assert sleeps == [0.05, 0.1]  # exponential (jitter pinned to its maximum)


def test_persistent_unprocessed_items_raise_after_five_attempts() -> None:
    stub = _StubDynamo(0, always=True)
    writer = BatchWriter(stub, "T", sleep=lambda _: None)  # type: ignore[arg-type]
    writer.put(_item(1))
    with pytest.raises(UnprocessedItemsError):
        writer.flush()
    assert len(stub.requests) == 5


def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    writer = BatchWriter(
        _StubDynamo(0, always=True),  # type: ignore[arg-type]
        "T",
        max_attempts=10,
        max_delay=0.3,
        sleep=sleeps.append,
        jitter=lambda: 1.0,
    )
    writer.put(_item(1))
    with pytest.raises(UnprocessedItemsError):
        writer.flush()
    assert max(sleeps) == 0.3

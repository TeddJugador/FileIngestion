"""The Lambda entry point: SQS batch handling, idempotency, partial failures."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest
from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError

from ingestion.notifications import S3ObjectRef
from tests.unit.aws import Harness
from tests.unit.conftest import fixture_bytes


@dataclass
class FakeContext:
    function_name: str = "etl_worker"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:etl_worker"
    aws_request_id: str = "req-1"

    def get_remaining_time_in_millis(self) -> int:
        return 290_000


def _sqs_event(*bodies: str) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": f"msg-{i}",
                "receiptHandle": f"rh-{i}",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "1"},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:file-processing-queue",
                "awsRegion": "us-east-1",
            }
            for i, body in enumerate(bodies)
        ]
    }


def _notification(ref: S3ObjectRef) -> str:
    encoded_key = ref.key.replace(" ", "+")
    return json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "eventTime": ref.event_time.isoformat().replace("+00:00", "Z"),
                    "s3": {
                        "bucket": {"name": ref.bucket},
                        "object": {
                            "key": encoded_key,
                            "size": ref.size,
                            "eTag": ref.etag,
                            "versionId": ref.version_id,
                        },
                    },
                }
            ]
        }
    )


@pytest.fixture
def worker(harness: Harness) -> Iterator[Any]:
    from ingestion.handlers import etl_worker

    etl_worker.runtime.cache_clear()
    # Route the handler's published events through the harness spy.
    with mock.patch.object(etl_worker, "EventPublisher") as publisher_cls:
        from ingestion.events import EventPublisher

        publisher_cls.side_effect = lambda _client, bus: EventPublisher(harness.events, bus)  # type: ignore[arg-type]
        yield etl_worker
    etl_worker.runtime.cache_clear()


def test_valid_upload_succeeds(harness: Harness, worker: Any) -> None:
    ref = harness.upload("raw/marks/SCH-001/term 4.csv", fixture_bytes("marks_valid.csv"))
    response = worker.handler(_sqs_event(_notification(ref)), FakeContext())
    assert response == {"batchItemFailures": []}
    assert len(harness.events.details("ImportCompleted")) == 1


def test_bad_file_is_reported_and_not_retried(harness: Harness, worker: Any) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_header_mismatch.csv"))
    response = worker.handler(_sqs_event(_notification(ref)), FakeContext())
    assert response == {"batchItemFailures": []}  # business outcome: success
    (event,) = harness.events.details("ImportFailed")
    assert event["data"]["reasonCode"] == "HEADER_MISMATCH"


def test_s3_test_event_is_ignored(harness: Harness, worker: Any) -> None:
    body = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "b"})
    assert worker.handler(_sqs_event(body), FakeContext()) == {"batchItemFailures": []}
    assert harness.events.entries == []


def test_redelivery_of_the_same_version_is_not_processed_twice(
    harness: Harness, worker: Any
) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    event = _sqs_event(_notification(ref))
    worker.handler(event, FakeContext())
    worker.handler(event, FakeContext())
    assert len(harness.events.details("ImportCompleted")) == 1
    assert len(harness.items_for_import(ref.import_id)) == 3


def test_reupload_of_identical_content_is_a_new_import(harness: Harness, worker: Any) -> None:
    first = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    second = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    assert first.etag == second.etag
    worker.handler(_sqs_event(_notification(first)), FakeContext())
    worker.handler(_sqs_event(_notification(second)), FakeContext())
    events = harness.events.details("ImportCompleted")
    assert len(events) == 2
    assert events[0]["data"]["importId"] != events[1]["data"]["importId"]


def test_single_failing_message_errors_the_invocation(harness: Harness, worker: Any) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    no_version = _notification(ref).replace(f'"versionId": "{ref.version_id}"', '"versionId": ""')
    with pytest.raises(BatchProcessingError):
        worker.handler(_sqs_event(no_version), FakeContext())
    assert harness.events.entries == []


def test_unexpected_errors_fail_only_that_message(harness: Harness, worker: Any) -> None:
    good = harness.upload("raw/marks/SCH-001/good.csv", fixture_bytes("marks_valid.csv"))
    no_version = _notification(good).replace(f'"versionId": "{good.version_id}"', '"versionId": ""')
    response = worker.handler(_sqs_event(_notification(good), no_version), FakeContext())
    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}


def test_persistent_unprocessed_items_fail_the_message_and_allow_a_retry(
    harness: Harness, worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingestion.storage import batch_writer

    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    real_flush = batch_writer.BatchWriter.flush

    def always_unprocessed(self: batch_writer.BatchWriter) -> None:
        raise batch_writer.UnprocessedItemsError(3)

    monkeypatch.setattr(batch_writer.BatchWriter, "flush", always_unprocessed)
    event = _sqs_event(_notification(ref))
    # With batch size 1 the whole batch failed, so Powertools raises: the invocation
    # errors (feeding the worker Errors alarm) and SQS redelivers the message.
    with pytest.raises(BatchProcessingError):
        worker.handler(event, FakeContext())
    assert harness.events.entries == []
    assert harness.open_multipart_uploads() == []

    # The failed attempt left no idempotency record, so SQS's retry processes the file.
    monkeypatch.setattr(batch_writer.BatchWriter, "flush", real_flush)
    assert worker.handler(event, FakeContext()) == {"batchItemFailures": []}
    assert len(harness.events.details("ImportCompleted")) == 1


def test_logs_use_the_key_as_correlation_id(harness: Harness, worker: Any) -> None:
    # The module-level logger captured the real stdout at import; redirect its handler.
    stream = io.StringIO()
    previous = worker.logger.registered_handler.setStream(stream)
    try:
        ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
        worker.handler(_sqs_event(_notification(ref)), FakeContext())
    finally:
        worker.logger.registered_handler.setStream(previous)
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{")]
    processed = [line for line in lines if line.get("message") == "file processed"]
    assert processed
    assert processed[0]["correlation_id"] == ref.key
    assert processed[0]["import_id"] == ref.import_id

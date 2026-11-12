from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest import mock

import boto3
import pytest
from botocore.stub import Stubber

from knowledge.jobs import InvalidJobError, JobKind, parse_job
from knowledge.vector_store import S3VectorsStore, VectorRecord, VectorStoreError
from tests.unit.aws import REGION
from tests.unit.knowledge.indexing_harness import (
    BUS,
    INGESTION_BUCKET,
    KNOWLEDGE_BUCKET,
    KnowledgeHarness,
    policy_markdown,
)

# --- message parsing ---------------------------------------------------------------------


def accepted_event(job: Any) -> dict[str, Any]:
    """The SQS body EventBridge delivers for a DocumentAccepted event."""
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": "DocumentAccepted",
        "source": "school.ingestion",
        "account": "123456789012",
        "time": "2026-11-12T08:00:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": {
            "metadata": {
                "eventId": "01JD0000000000000000000000",
                "schemaVersion": "1.0",
                "occurredAt": job.accepted_at,
                "correlationId": job.import_id,
                "schoolId": job.school_id,
                "producer": "ingestion-service",
            },
            "data": {
                "documentId": job.document_id,
                "version": job.version,
                "importId": job.import_id,
                "title": job.title,
                "audience": list(job.audience),
                "contentType": "text/markdown",
                "sizeBytes": 100,
                "sha256": job.sha256,
                "sourceFileKey": "raw/documents/SCH-001/policy.md",
                "sourceVersionId": "abc",
                "stagedKey": job.staged_key,
                "uploadedBy": "USR-admin-0001",
            },
        },
    }


def test_document_accepted_event_parses(kh: KnowledgeHarness) -> None:
    job = kh.stage(policy_markdown("T"))
    parsed = parse_job(json.dumps(accepted_event(job)))
    assert parsed == job
    assert parsed.kind is JobKind.ACCEPTED
    assert parsed.correlation_id == f"{job.document_id}#{job.version}"


def test_reindex_message_parses() -> None:
    parsed = parse_job(
        json.dumps(
            {
                "kind": "REINDEX",
                "schoolId": "SCH-001",
                "documentId": "doc-4be1a07c93d2",
                "version": "v-9f2c41e07ab3",
                "sourceKey": "sources/SCH-001/doc-4be1a07c93d2/v-9f2c41e07ab3/source.pdf",
                "title": "Fee Policy",
                "audience": ["PARENT"],
                "importId": "imp-1",
            }
        )
    )
    assert parsed.kind is JobKind.REINDEX
    assert parsed.extension == "pdf"
    assert parsed.audience == ("PARENT",)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps({"detail-type": "ImportCompleted", "source": "school.ingestion", "detail": {}}),
        json.dumps({"kind": "REINDEX", "schoolId": "SCH-1"}),
    ],
)
def test_unrecognised_messages_are_rejected(body: str) -> None:
    with pytest.raises(InvalidJobError):
        parse_job(body)


def test_events_from_another_source_are_rejected(kh: KnowledgeHarness) -> None:
    event = accepted_event(kh.stage(policy_markdown("T")))
    event["source"] = "school.knowledge"  # only ingestion may announce documents
    with pytest.raises(InvalidJobError):
        parse_job(json.dumps(event))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audience", ["TEACHER"]),
        ("audience", []),
        ("documentId", "doc-XYZ"),
        ("stagedKey", "x/source.exe"),
    ],
)
def test_invalid_event_fields_are_rejected(kh: KnowledgeHarness, field: str, value: Any) -> None:
    event = accepted_event(kh.stage(policy_markdown("T")))
    event["detail"]["data"][field] = value
    with pytest.raises(InvalidJobError):
        parse_job(json.dumps(event))


# --- S3 Vectors adapter (stubbed against the real API model) ------------------------------


def _stubbed(batch_size: int = 500) -> tuple[S3VectorsStore, Stubber]:
    client = boto3.client(
        "s3vectors", region_name=REGION, aws_access_key_id="x", aws_secret_access_key="x"
    )
    return S3VectorsStore(
        client, bucket_name="school-vectors", index_name="docs-gemini-768-v1", batch_size=batch_size
    ), Stubber(client)


def _record(i: int) -> VectorRecord:
    return VectorRecord(
        key=f"doc-1#v-1#{i:05d}", vector=[0.5, 0.25], metadata={"school_id": "SCH-001"}
    )


def test_put_vectors_request_shape_and_batching() -> None:
    store, stubber = _stubbed(batch_size=2)
    for batch in ([0, 1], [2]):
        stubber.add_response(
            "put_vectors",
            {},
            {
                "vectorBucketName": "school-vectors",
                "indexName": "docs-gemini-768-v1",
                "vectors": [
                    {
                        "key": f"doc-1#v-1#{i:05d}",
                        "data": {"float32": [0.5, 0.25]},
                        "metadata": {"school_id": "SCH-001"},
                    }
                    for i in batch
                ],
            },
        )
    with stubber:
        store.put([_record(i) for i in range(3)])
    stubber.assert_no_pending_responses()


def test_delete_vectors_batches_of_500() -> None:
    store, stubber = _stubbed()
    keys = [f"k{i}" for i in range(501)]
    stubber.add_response(
        "delete_vectors",
        {},
        {
            "vectorBucketName": "school-vectors",
            "indexName": "docs-gemini-768-v1",
            "keys": keys[:500],
        },
    )
    stubber.add_response(
        "delete_vectors",
        {},
        {
            "vectorBucketName": "school-vectors",
            "indexName": "docs-gemini-768-v1",
            "keys": keys[500:],
        },
    )
    with stubber:
        store.delete(keys)
    stubber.assert_no_pending_responses()


def test_vector_store_errors_are_wrapped() -> None:
    store, stubber = _stubbed()
    stubber.add_client_error(
        "put_vectors", service_error_code="ServiceUnavailableException", http_status_code=503
    )
    with stubber, pytest.raises(VectorStoreError):
        store.put([_record(0)])


def test_batch_size_is_limited_to_the_service_maximum() -> None:
    with pytest.raises(ValueError, match="1-500"):
        _stubbed(batch_size=501)


# --- handler ------------------------------------------------------------------------------


@pytest.fixture
def indexer_handler(kh: KnowledgeHarness, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name, value in {
        "VECTOR_INDEX_NAME": "docs-gemini-768-v1",
        "VECTOR_BUCKET_NAME": "school-knowledge-vectors-123456789012-us-east-1",
        "REGISTRY_TABLE_NAME": "DocumentRegistry",
        "IDEMPOTENCY_TABLE_NAME": "KnowledgeIdempotency",
        "KNOWLEDGE_BUCKET_NAME": KNOWLEDGE_BUCKET,
        "INGESTION_BUCKET_NAME": INGESTION_BUCKET,
        "EVENT_BUS_NAME": BUS,
        "MAX_DOCUMENT_BYTES": str(25 * 1024 * 1024),
        "EMBEDDING_PROVIDER": "gemini",
        "EMBEDDING_MODEL": "gemini-embedding-2",
        "EMBEDDING_DIMENSION": "768",
        "POWERTOOLS_METRICS_NAMESPACE": "SchoolSystem/Knowledge",
    }.items():
        monkeypatch.setenv(name, value)
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="KnowledgeIdempotency",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    from knowledge.handlers import indexer as module

    module.runtime.cache_clear()
    with (
        mock.patch.object(module, "build_embedder", return_value=kh.embedder),
        mock.patch.object(module, "S3VectorsStore", return_value=kh.store),
        mock.patch.object(
            module,
            "EventPublisher",
            lambda _client, bus: __import__("knowledge.events").events.EventPublisher(
                kh.events, bus
            ),
        ),
    ):
        yield module
    module.runtime.cache_clear()


def _sqs(body: dict[str, Any], receive_count: int = 1) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": "m-1",
                "receiptHandle": "rh",
                "body": json.dumps(body),
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:document-indexing-queue",
                "awsRegion": "us-east-1",
            }
        ]
    }


def test_handler_indexes_and_is_idempotent_on_redelivery(
    kh: KnowledgeHarness, indexer_handler: Any
) -> None:
    from tests.unit.test_handler import FakeContext

    job = kh.stage(policy_markdown("Code of Conduct 2027"))
    event = _sqs(accepted_event(job))
    assert indexer_handler.handler(event, FakeContext()) == {"batchItemFailures": []}
    assert indexer_handler.handler(event, FakeContext()) == {"batchItemFailures": []}
    assert len(kh.events_of("DocumentIndexed")) == 1
    calls_after_first = len(kh.embedder.calls)
    indexer_handler.handler(event, FakeContext())
    assert len(kh.embedder.calls) == calls_after_first


def test_handler_rejects_unknown_messages_into_the_dlq(
    kh: KnowledgeHarness, indexer_handler: Any
) -> None:
    from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError

    from tests.unit.test_handler import FakeContext

    with pytest.raises(BatchProcessingError):  # whole (single-message) batch failed
        indexer_handler.handler(_sqs({"hello": "world"}), FakeContext())


def test_missing_s3vectors_client_fails_loudly() -> None:
    from knowledge.handlers.indexer import MissingS3VectorsClientError, require_s3vectors

    session = mock.Mock()
    session.get_available_services.return_value = ["s3", "dynamodb"]
    with pytest.raises(MissingS3VectorsClientError):
        require_s3vectors(session)


def test_empty_put_or_delete_makes_no_request() -> None:
    """The API rejects empty lists (min 1), so nothing is sent for nothing."""
    store, stubber = _stubbed()
    with stubber:  # no responses queued: any call would fail
        store.put([])
        store.delete([])

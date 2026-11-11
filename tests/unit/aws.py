"""moto-backed test harness for the pipeline and handler."""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from aws_lambda_powertools import Logger
from moto import mock_aws

from ingestion.events import EventPublisher
from ingestion.notifications import S3ObjectRef
from ingestion.pipeline import Pipeline, Services

REGION = "us-east-1"
BUCKET = "school-ingestion-123456789012-us-east-1"
RECORDS_TABLE = "IngestedRecords"
IDEMPOTENCY_TABLE = "IngestionIdempotency"
BUS = "SchoolEventBus"
EVENT_TIME = datetime(2026, 11, 11, 8, 0, 0, tzinfo=UTC)
UPLOADER = "USR-teacher-0042"


class SpyEvents:
    """Delegates to the real (moto) client and keeps every published entry."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.entries: list[dict[str, Any]] = []

    def put_events(self, **kwargs: Any) -> Any:
        response = self._client.put_events(**kwargs)
        self.entries.extend(kwargs["Entries"])
        return response

    def details(self, detail_type: str | None = None) -> list[dict[str, Any]]:
        return [
            json.loads(e["Detail"])
            for e in self.entries
            if detail_type is None or e["DetailType"] == detail_type
        ]


@dataclass
class RecordingMetrics:
    records: list[tuple[str, float, str, dict[str, str]]] = field(default_factory=list)

    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        self.records.append((name, value, unit, dict(dimensions)))

    def value(self, name: str) -> float:
        return sum(v for n, v, _, _ in self.records if n == name)


@dataclass
class Harness:
    s3: Any
    dynamodb: Any
    events: SpyEvents
    metrics: RecordingMetrics
    logger: Logger
    log_stream: io.StringIO

    def logs(self) -> str:
        """Everything the pipeline logged, as the JSON lines Powertools would emit."""
        return self.log_stream.getvalue()

    def pipeline(self, **overrides: Any) -> Pipeline:
        services = Services(
            s3=overrides.get("s3", self.s3),
            dynamodb=overrides.get("dynamodb", self.dynamodb),
            publisher=overrides.get("publisher", EventPublisher(self.events, BUS)),  # type: ignore[arg-type]
            metrics=self.metrics,
            logger=self.logger,
            records_table=RECORDS_TABLE,
        )
        return Pipeline(services)

    def upload(
        self,
        key: str,
        body: bytes,
        *,
        uploaded_by: str | None = UPLOADER,
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectRef:
        meta = dict(metadata or {})
        if uploaded_by is not None:
            meta["uploaded-by"] = uploaded_by
        response = self.s3.put_object(Bucket=BUCKET, Key=key, Body=body, Metadata=meta)
        return S3ObjectRef(
            bucket=BUCKET,
            key=key,
            version_id=response["VersionId"],
            size=len(body),
            etag=response["ETag"].strip('"'),
            event_time=EVENT_TIME,
        )

    def get_json(self, key: str) -> Any:
        return json.loads(self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())

    def get_lines(self, key: str) -> list[dict[str, Any]]:
        body = self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
        return [json.loads(line) for line in body.splitlines()]

    def exists(self, key: str) -> bool:
        return "Contents" in self.s3.list_objects_v2(Bucket=BUCKET, Prefix=key)

    def object_keys(self, prefix: str = "processed/") -> list[str]:
        response = self.s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        return sorted(o["Key"] for o in response.get("Contents", []))

    def items_for_import(self, import_id: str) -> list[dict[str, Any]]:
        response = self.dynamodb.query(
            TableName=RECORDS_TABLE,
            IndexName="ByImport",
            KeyConditionExpression="import_id = :i",
            ExpressionAttributeValues={":i": {"S": import_id}},
        )
        return list(response["Items"])

    def open_multipart_uploads(self) -> list[Any]:
        return list(self.s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []))


def aws_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "POWERTOOLS_TRACE_DISABLED": "true",
        "POWERTOOLS_SERVICE_NAME": "ingestion",
        "POWERTOOLS_METRICS_NAMESPACE": "SchoolSystem/Ingestion",
        "RECORDS_TABLE_NAME": RECORDS_TABLE,
        "IDEMPOTENCY_TABLE_NAME": IDEMPOTENCY_TABLE,
        "EVENT_BUS_NAME": BUS,
        "BUCKET_NAME": BUCKET,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def create_resources() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=RECORDS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "import_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "ByImport",
                "KeySchema": [
                    {"AttributeName": "import_id", "KeyType": "HASH"},
                    {"AttributeName": "PK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    ddb.create_table(
        TableName=IDEMPOTENCY_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    boto3.client("events", region_name=REGION).create_event_bus(Name=BUS)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    aws_environment(monkeypatch)
    with mock_aws():
        create_resources()
        stream = io.StringIO()
        # A unique service name gives a fresh logger with its own handler per test.
        logger = Logger(
            service=f"ingestion-test-{uuid.uuid4().hex[:8]}",
            logger_handler=logging.StreamHandler(stream),
        )
        yield Harness(
            s3=boto3.client("s3", region_name=REGION),
            dynamodb=boto3.client("dynamodb", region_name=REGION),
            events=SpyEvents(boto3.client("events", region_name=REGION)),
            metrics=RecordingMetrics(),
            logger=logger,
            log_stream=stream,
        )

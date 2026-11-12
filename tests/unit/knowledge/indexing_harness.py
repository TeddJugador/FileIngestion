"""moto-backed harness for the indexer: two buckets, the registry, the bus, an in-memory
vector store and the fake embedder."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from aws_lambda_powertools import Logger
from moto import mock_aws

from knowledge.events import EventPublisher
from knowledge.indexer import Indexer, IndexerSettings
from knowledge.jobs import IndexJob, JobKind
from knowledge.providers.fakes import FakeEmbedder
from knowledge.registry import Registry
from knowledge.sources import SourceStore
from knowledge.vector_store import InMemoryVectorStore, VectorStore
from tests.unit.aws import REGION, SpyEvents, aws_environment

INGESTION_BUCKET = "school-ingestion-123456789012-us-east-1"
KNOWLEDGE_BUCKET = "school-knowledge-docs-123456789012-us-east-1"
REGISTRY_TABLE = "DocumentRegistry"
BUS = "SchoolEventBus"
INDEX = "docs-gemini-768-v1"
SCHOOL = "SCH-001"
DOC = "doc-4be1a07c93d2"
MAX_BYTES = 25 * 1024 * 1024


def policy_markdown(title: str, sections: int = 3, sentences: int = 40, marker: str = "") -> bytes:
    lines = [f"# {title}", ""]
    for s in range(sections):
        lines += [f"## Section {s + 1}", ""]
        for p in range(sentences // 8):
            lines.append(
                " ".join(
                    f"Rule {s}.{p}.{i} {marker} says learners and parents follow school policy "
                    f"number {i} during term time."
                    for i in range(8)
                )
            )
            lines.append("")
    return "\n".join(lines).encode()


@dataclass
class RecordingMetrics:
    records: list[tuple[str, float, str, dict[str, str]]] = field(default_factory=list)

    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        self.records.append((name, value, unit, dict(dimensions)))

    def value(self, name: str) -> float:
        return sum(v for n, v, _, _ in self.records if n == name)


@dataclass
class KnowledgeHarness:
    s3: Any
    dynamodb: Any
    events: SpyEvents
    store: InMemoryVectorStore
    embedder: FakeEmbedder
    metrics: RecordingMetrics
    logger: Logger
    log_stream: io.StringIO

    def indexer(
        self,
        *,
        index_name: str = INDEX,
        store: VectorStore | None = None,
        embedder: Any = None,
    ) -> Indexer:
        embedder = embedder or self.embedder
        return Indexer(
            settings=IndexerSettings(
                index_name=index_name,
                embedding_model_id=embedder.model_id,
                embedding_dimension=embedder.dimension,
            ),
            registry=self.registry,
            sources=SourceStore(
                self.s3,
                knowledge_bucket=KNOWLEDGE_BUCKET,
                ingestion_bucket=INGESTION_BUCKET,
                max_bytes=MAX_BYTES,
            ),
            store=store or self.store,
            embedder=embedder,
            publisher=EventPublisher(self.events, BUS),
            metrics=self.metrics,
            logger=self.logger,
        )

    @property
    def registry(self) -> Registry:
        return Registry(self.dynamodb, REGISTRY_TABLE)

    def stage(
        self,
        content: bytes,
        *,
        extension: str = "md",
        document_id: str = DOC,
        accepted_at: str = "2026-11-12T08:00:00.000Z",
        audience: tuple[str, ...] = ("STAFF", "PARENT"),
        title: str = "Code of Conduct 2027",
        upload: bool = True,
    ) -> IndexJob:
        """Put ``content`` where ingestion stages it and return the matching job."""
        sha = hashlib.sha256(content).hexdigest()
        version = f"v-{sha[:12]}"
        staged_key = f"processed/documents/{SCHOOL}/{document_id}/{version}/source.{extension}"
        if upload:
            self.s3.put_object(Bucket=INGESTION_BUCKET, Key=staged_key, Body=content)
        return IndexJob(
            kind=JobKind.ACCEPTED,
            school_id=SCHOOL,
            document_id=document_id,
            version=version,
            import_id=f"imp-{sha[:12]}",
            title=title,
            audience=audience,
            accepted_at=accepted_at,
            staged_key=staged_key,
            sha256=sha,
        )

    def reindex_job(self, accepted: IndexJob) -> IndexJob:
        return IndexJob(
            kind=JobKind.REINDEX,
            school_id=accepted.school_id,
            document_id=accepted.document_id,
            version=accepted.version,
            import_id=accepted.import_id,
            title=accepted.title,
            audience=accepted.audience,
            source_key=f"sources/{SCHOOL}/{accepted.document_id}/{accepted.version}/source.{accepted.extension}",
        )

    def knowledge_keys(self, prefix: str = "") -> list[str]:
        response = self.s3.list_objects_v2(Bucket=KNOWLEDGE_BUCKET, Prefix=prefix)
        return sorted(o["Key"] for o in response.get("Contents", []))

    def read_json(self, key: str) -> Any:
        return json.loads(self.s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key=key)["Body"].read())

    def events_of(self, detail_type: str) -> list[dict[str, Any]]:
        return self.events.details(detail_type)

    def logs(self) -> str:
        return self.log_stream.getvalue()


def create_knowledge_resources() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    for bucket in (INGESTION_BUCKET, KNOWLEDGE_BUCKET):
        s3.create_bucket(Bucket=bucket)
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=REGISTRY_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "school_id", "AttributeType": "S"},
            {"AttributeName": "status_updated_at", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "BySchoolStatus",
                "KeySchema": [
                    {"AttributeName": "school_id", "KeyType": "HASH"},
                    {"AttributeName": "status_updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    boto3.client("events", region_name=REGION).create_event_bus(Name=BUS)


@pytest.fixture
def kh(monkeypatch: pytest.MonkeyPatch) -> Iterator[KnowledgeHarness]:
    aws_environment(monkeypatch)
    with mock_aws():
        create_knowledge_resources()
        stream = io.StringIO()
        yield KnowledgeHarness(
            s3=boto3.client("s3", region_name=REGION),
            dynamodb=boto3.client("dynamodb", region_name=REGION),
            events=SpyEvents(boto3.client("events", region_name=REGION)),
            store=InMemoryVectorStore(batch_size=10),
            embedder=FakeEmbedder(),
            metrics=RecordingMetrics(),
            logger=Logger(
                service=f"knowledge-test-{uuid.uuid4().hex[:8]}",
                logger_handler=logging.StreamHandler(stream),
            ),
            log_stream=stream,
        )

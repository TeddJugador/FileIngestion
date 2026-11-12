"""Builds a QueryService over the indexing harness, with documents indexed for real."""

from __future__ import annotations

import boto3

from knowledge.identity import Caller, Role
from knowledge.jobs import IndexJob
from knowledge.providers.fakes import FakeGenerator
from knowledge.query import QueryService, QuerySettings
from knowledge.query_log import QueryLog
from knowledge.sources import SourceStore
from knowledge.versions import VersionCleaner
from tests.unit.aws import REGION
from tests.unit.knowledge.indexing_harness import (
    INDEX,
    INGESTION_BUCKET,
    KNOWLEDGE_BUCKET,
    MAX_BYTES,
    KnowledgeHarness,
)

QUERY_LOG_TABLE = "QueryLog"
PARENT = Caller(user_id="user-parent-1", school_id="SCH-001", role=Role.PARENT)
STAFF = Caller(user_id="user-staff-1", school_id="SCH-001", role=Role.STAFF)
ADMIN = Caller(user_id="user-admin-1", school_id="SCH-001", role=Role.ADMIN)


def create_query_log() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=QUERY_LOG_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "queryId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "queryId", "KeyType": "HASH"}],
    )


def query_service(
    kh: KnowledgeHarness,
    *,
    generator: FakeGenerator | None = None,
    threshold: float = 0.05,
    embedding_model_id: str | None = None,
) -> QueryService:
    sources = SourceStore(
        kh.s3,
        knowledge_bucket=KNOWLEDGE_BUCKET,
        ingestion_bucket=INGESTION_BUCKET,
        max_bytes=MAX_BYTES,
    )
    return QueryService(
        settings=QuerySettings(
            index_name=INDEX,
            embedding_model_id=embedding_model_id or kh.embedder.model_id,
            generation_provider="FAKE",
            generation_model="fake/echo",
            similarity_threshold=threshold,
        ),
        registry=kh.registry,
        store=kh.store,
        embedder=kh.embedder,
        generator=generator or FakeGenerator(),
        publisher=__import__("knowledge.events").events.EventPublisher(kh.events, "SchoolEventBus"),
        query_log=QueryLog(kh.dynamodb, QUERY_LOG_TABLE),
        cleaner=VersionCleaner(kh.registry, sources, kh.store, INDEX),
        metrics=kh.metrics,
        logger=kh.logger,
    )


def index_text(
    kh: KnowledgeHarness,
    text: str,
    *,
    document_id: str,
    audience: tuple[str, ...],
    title: str,
    school_id: str = "SCH-001",
    accepted_at: str = "2026-11-12T08:00:00.000Z",
) -> IndexJob:
    job = kh.stage(
        text.encode(),
        document_id=document_id,
        audience=audience,
        title=title,
        accepted_at=accepted_at,
    )
    if school_id != job.school_id:
        job = _move_school(kh, job, school_id)
    kh.indexer().process(job)
    return job


def _move_school(kh: KnowledgeHarness, job: IndexJob, school_id: str) -> IndexJob:
    staged = job.staged_key.replace(job.school_id, school_id)
    kh.s3.copy_object(
        Bucket=INGESTION_BUCKET,
        Key=staged,
        CopySource={"Bucket": INGESTION_BUCKET, "Key": job.staged_key},
    )
    return IndexJob(**{**job.__dict__, "school_id": school_id, "staged_key": staged})

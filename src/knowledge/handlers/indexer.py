"""SQS-triggered indexer (batch size 1). Wiring, Powertools and idempotency only."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config

from knowledge.events import EventPublisher
from knowledge.indexer import Indexer, IndexerSettings
from knowledge.jobs import IndexJob, parse_job
from knowledge.metrics import PowertoolsMetricsSink
from knowledge.providers.factory import ProviderSettings, build_embedder
from knowledge.providers.http import INDEXING_RETRIES
from knowledge.registry import Registry
from knowledge.sources import SourceStore
from knowledge.vector_store import S3VectorsStore

logger = Logger()
tracer = Tracer()
processor = BatchProcessor(event_type=EventType.SQS)
AWS_RETRIES = Config(retries={"mode": "standard", "max_attempts": 5})


class MissingS3VectorsClientError(RuntimeError):
    """The runtime's boto3 predates S3 Vectors (spec: check, bundle a newer one if so)."""


@dataclass(frozen=True)
class Runtime:
    config: IdempotencyConfig
    process: Callable[..., dict[str, Any]]


def require_s3vectors(session: boto3.session.Session) -> None:
    if "s3vectors" not in session.get_available_services():
        raise MissingS3VectorsClientError(
            "boto3 in this runtime has no s3vectors client; bundle a newer boto3 in the layer"
        )


@cache
def runtime() -> Runtime:
    env = os.environ
    session = boto3.session.Session()
    require_s3vectors(session)
    metrics = PowertoolsMetricsSink(env["POWERTOOLS_METRICS_NAMESPACE"])
    provider_settings = ProviderSettings.from_env(env)
    indexer = Indexer(
        settings=IndexerSettings(
            index_name=env["VECTOR_INDEX_NAME"],
            embedding_model_id=provider_settings.embedding_model_id,
            embedding_dimension=provider_settings.embedding_dimension,
            max_receive_count=int(env.get("MAX_RECEIVE_COUNT", "3")),
        ),
        registry=Registry(
            session.client("dynamodb", config=AWS_RETRIES), env["REGISTRY_TABLE_NAME"]
        ),
        sources=SourceStore(
            session.client("s3", config=AWS_RETRIES),
            knowledge_bucket=env["KNOWLEDGE_BUCKET_NAME"],
            ingestion_bucket=env["INGESTION_BUCKET_NAME"],
            max_bytes=int(env["MAX_DOCUMENT_BYTES"]),
        ),
        store=S3VectorsStore(
            session.client("s3vectors", config=AWS_RETRIES),
            bucket_name=env["VECTOR_BUCKET_NAME"],
            index_name=env["VECTOR_INDEX_NAME"],
            batch_size=int(env.get("VECTOR_PUT_BATCH_SIZE", "500")),
        ),
        embedder=build_embedder(provider_settings, policy=INDEXING_RETRIES, observer=metrics),
        publisher=EventPublisher(session.client("events"), env["EVENT_BUS_NAME"]),
        metrics=metrics,
        logger=logger,
    )
    config = IdempotencyConfig(
        event_key_jmespath="idempotency_key",
        raise_on_no_idempotency_key=True,
        expires_after_seconds=24 * 60 * 60,
    )
    persistence = DynamoDBPersistenceLayer(table_name=env["IDEMPOTENCY_TABLE_NAME"])

    @tracer.capture_method
    def process(job: dict[str, Any]) -> dict[str, Any]:
        result = indexer.process(
            IndexJob.from_payload(job["job"]), receive_count=job["receive_count"]
        )
        return result.to_dict()

    wrapped = idempotent_function(
        data_keyword_argument="job", config=config, persistence_store=persistence
    )(process)
    return Runtime(config=config, process=wrapped)


def record_handler(record: SQSRecord) -> None:
    job = parse_job(record.body)
    logger.set_correlation_id(job.correlation_id)
    index_name = os.environ["VECTOR_INDEX_NAME"]
    result = runtime().process(
        job={
            # Same version into a new index is new work, so the index is part of the key.
            "idempotency_key": f"{job.document_id}#{job.version}#{index_name}",
            "job": job.to_payload(),
            "receive_count": int(record.attributes.approximate_receive_count),
        }
    )
    logger.info("indexing message processed", outcome=result["outcome"], reason=result["reason"])


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    runtime().config.register_lambda_context(context)
    return dict(
        process_partial_response(
            event=event, record_handler=record_handler, processor=processor, context=context
        )
    )

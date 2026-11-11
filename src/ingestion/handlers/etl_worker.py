"""SQS-triggered worker: one S3 object per message (batch size 1).

Kept thin: wiring, Powertools and idempotency. The logic lives in ``ingestion.pipeline``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.metrics import MetricUnit, single_metric
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

from ingestion.events import EventPublisher
from ingestion.notifications import S3ObjectRef, parse_notification
from ingestion.pipeline import Pipeline, Services

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

logger = Logger()
tracer = Tracer()
processor = BatchProcessor(event_type=EventType.SQS)


class PowertoolsMetricsSink:
    """One EMF document per metric, so each carries exactly its own dimensions."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        with single_metric(
            name=name, unit=MetricUnit(unit), value=value, namespace=self._namespace
        ) as metric:
            for key, dim_value in dimensions.items():
                metric.add_dimension(name=key, value=dim_value)


@dataclass(frozen=True)
class Runtime:
    config: IdempotencyConfig
    process_file: Callable[..., dict[str, Any]]


@cache
def runtime() -> Runtime:
    """Built on first use (not at import) so clients pick up the environment, and
    tests can reset it with ``runtime.cache_clear()``."""
    config = IdempotencyConfig(
        event_key_jmespath="import_id",
        raise_on_no_idempotency_key=True,
        expires_after_seconds=IDEMPOTENCY_TTL_SECONDS,
    )
    persistence = DynamoDBPersistenceLayer(table_name=os.environ["IDEMPOTENCY_TABLE_NAME"])
    pipeline = Pipeline(
        Services(
            s3=boto3.client("s3"),
            dynamodb=boto3.client("dynamodb"),
            publisher=EventPublisher(boto3.client("events"), os.environ["EVENT_BUS_NAME"]),
            metrics=PowertoolsMetricsSink(os.environ["POWERTOOLS_METRICS_NAMESPACE"]),
            logger=logger,
            records_table=os.environ["RECORDS_TABLE_NAME"],
        )
    )

    @tracer.capture_method
    def process_file(file: dict[str, Any]) -> dict[str, Any]:
        return pipeline.process(S3ObjectRef.from_payload(file)).to_dict()

    wrapped = idempotent_function(
        data_keyword_argument="file", config=config, persistence_store=persistence
    )(process_file)
    return Runtime(config=config, process_file=wrapped)


def record_handler(record: SQSRecord) -> None:
    for ref in parse_notification(record.body):  # [] for s3:TestEvent
        logger.set_correlation_id(ref.key)
        result = runtime().process_file(file=ref.to_payload())
        logger.info("file processed", status=result["status"], reason_code=result["reasonCode"])


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    runtime().config.register_lambda_context(context)
    response = process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
    return dict(response)

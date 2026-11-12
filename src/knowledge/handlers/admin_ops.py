"""Manually invoked knowledge admin tool. Payloads::

{"action": "reindex", "schoolId": "SCH-001", "dryRun": true}   # schoolId optional
{"action": "inspect_dlq", "max": 10}
{"action": "redrive_dlq", "maxPerSecond": 10}
{"action": "dlq_status"}
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from ingestion.replay import DlqOperations
from knowledge.admin_ops import REQUEST_ADAPTER, AdminOps, describe_indexing_message
from knowledge.registry import Registry

logger = Logger()


class InvalidRequestError(ValueError):
    pass


@cache
def operations() -> AdminOps:
    env = os.environ
    sqs = boto3.client("sqs")
    return AdminOps(
        registry=Registry(boto3.client("dynamodb"), env["REGISTRY_TABLE_NAME"]),
        sqs=sqs,
        queue_url=env["INDEXING_QUEUE_URL"],
        index_name=env["VECTOR_INDEX_NAME"],
        dlq=DlqOperations(
            sqs,
            dlq_url=env["INDEXING_DLQ_URL"],
            dlq_arn=env["INDEXING_DLQ_ARN"],
            main_queue_arn=env["INDEXING_QUEUE_ARN"],
            describe_body=describe_indexing_message,
        ),
        logger=logger,
        default_rate=float(env.get("REINDEX_RATE_PER_SECOND", "2")),
    )


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    try:
        request = REQUEST_ADAPTER.validate_python(event)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'payload'}: {e['msg']}" for e in exc.errors()
        )
        raise InvalidRequestError(
            f"{problems}. Expected one of: "
            '{"action": "reindex", "schoolId": "SCH-001" (optional), "dryRun": true|false}, '
            '{"action": "inspect_dlq", "max": 1-100}, '
            '{"action": "redrive_dlq", "maxPerSecond": 1-500}, '
            '{"action": "dlq_status"}'
        ) from None
    logger.info("admin operation requested", action=request.action)
    return operations().handle(request)

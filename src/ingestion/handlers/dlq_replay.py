"""Manually invoked DLQ tool.

Payloads::

    {"action": "inspect", "max": 10}
    {"action": "redrive", "maxPerSecond": 10}
    {"action": "status"}                       # latest redrive
    {"action": "status", "taskHandle": "..."}  # a running redrive
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from ingestion.replay import REQUEST_ADAPTER, DlqOperations

logger = Logger()


class InvalidRequestError(ValueError):
    """Bad payload; the message lists what's wrong and what's accepted."""


@cache
def operations() -> DlqOperations:
    return DlqOperations(
        boto3.client("sqs"),
        dlq_url=os.environ["DLQ_URL"],
        dlq_arn=os.environ["DLQ_ARN"],
        main_queue_arn=os.environ["MAIN_QUEUE_ARN"],
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
            '{"action": "inspect", "max": 1-100}, '
            '{"action": "redrive", "maxPerSecond": 1-500}, '
            '{"action": "status", "taskHandle": "... (optional)"}'
        ) from None
    logger.info("dlq replay request", action=request.action)
    result = operations().handle(request)
    logger.info("dlq replay done", action=request.action, returned=result.get("returned"))
    return result

"""DLQ operations for admins: inspect without deleting, redrive, check a redrive.

Invoked manually (console or CLI) through ``handlers/dlq_replay.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import unquote_plus

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ingestion.clock import iso_ms

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient
    from mypy_boto3_sqs.type_defs import ListMessageMoveTasksResultEntryTypeDef, MessageTypeDef

MAX_INSPECT = 100
SQS_MAX_BATCH = 10
INSPECT_VISIBILITY_SECONDS = 30  # backstop if resetting visibility fails


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InspectRequest(_Request):
    action: Literal["inspect"]
    max: Annotated[int, Field(ge=1, le=MAX_INSPECT)] = 10


class RedriveRequest(_Request):
    action: Literal["redrive"]
    maxPerSecond: Annotated[int, Field(ge=1, le=500)] = 10


class StatusRequest(_Request):
    action: Literal["status"]
    # Optional: AWS reports a task's handle only while it is RUNNING, so without one
    # (or once the task has finished) the latest task is shown.
    taskHandle: Annotated[str, Field(min_length=1)] | None = None


ReplayRequest = Annotated[
    InspectRequest | RedriveRequest | StatusRequest, Field(discriminator="action")
]
REQUEST_ADAPTER: TypeAdapter[InspectRequest | RedriveRequest | StatusRequest] = TypeAdapter(
    ReplayRequest
)


class DlqOperations:
    def __init__(self, sqs: SQSClient, *, dlq_url: str, dlq_arn: str, main_queue_arn: str) -> None:
        self._sqs = sqs
        self._dlq_url = dlq_url
        self._dlq_arn = dlq_arn
        self._main_arn = main_queue_arn

    def handle(self, request: InspectRequest | RedriveRequest | StatusRequest) -> dict[str, Any]:
        if isinstance(request, InspectRequest):
            return self.inspect(request.max)
        if isinstance(request, RedriveRequest):
            return self.redrive(request.maxPerSecond)
        return self.status(request.taskHandle)

    def inspect(self, limit: int) -> dict[str, Any]:
        """Peek at up to ``limit`` messages without deleting them.

        Received messages are made visible again straight away. Note that receiving a
        message, even to inspect it, increments its ``ApproximateReceiveCount``.
        """
        seen: dict[str, dict[str, Any]] = {}
        receipts: list[str] = []
        while len(seen) < limit:
            response = self._sqs.receive_message(
                QueueUrl=self._dlq_url,
                MaxNumberOfMessages=min(SQS_MAX_BATCH, limit - len(seen)),
                VisibilityTimeout=INSPECT_VISIBILITY_SECONDS,
                WaitTimeSeconds=1,
                MessageSystemAttributeNames=[
                    "ApproximateReceiveCount",
                    "ApproximateFirstReceiveTimestamp",
                    "SentTimestamp",
                ],
            )
            messages = response.get("Messages", [])
            if not messages:
                break
            for message in messages:
                receipts.append(message["ReceiptHandle"])
                seen.setdefault(message["MessageId"], _describe(message))
        self._release(receipts)

        depth = self._sqs.get_queue_attributes(
            QueueUrl=self._dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]["ApproximateNumberOfMessages"]
        return {
            "action": "inspect",
            "approximateDepth": int(depth),
            "returned": len(seen),
            "messages": list(seen.values()),
        }

    def redrive(self, max_per_second: int) -> dict[str, Any]:
        response = self._sqs.start_message_move_task(
            SourceArn=self._dlq_arn,
            DestinationArn=self._main_arn,
            MaxNumberOfMessagesPerSecond=max_per_second,
        )
        return {
            "action": "redrive",
            "taskHandle": response["TaskHandle"],
            "next": {"action": "status", "taskHandle": response["TaskHandle"]},
        }

    def status(self, task_handle: str | None = None) -> dict[str, Any]:
        """Report a redrive. ``ListMessageMoveTasks`` returns the most recent tasks first
        and includes ``TaskHandle`` only while a task is RUNNING, so a finished task can't
        be matched by handle: report the latest one instead, and say so."""
        results = self._sqs.list_message_move_tasks(SourceArn=self._dlq_arn, MaxResults=10).get(
            "Results", []
        )
        if task_handle is not None:
            for task in results:
                if task.get("TaskHandle") == task_handle:
                    return {"action": "status", "matchedHandle": True, "task": _task(task)}
        if not results:
            return {"action": "status", "matchedHandle": False, "task": None}
        response: dict[str, Any] = {
            "action": "status",
            "matchedHandle": False,
            "task": _task(results[0]),
        }
        if task_handle is not None:
            response["note"] = (
                "AWS reports a task's handle only while it is RUNNING; showing the most "
                "recent task instead."
            )
        return response

    def _release(self, receipts: list[str]) -> None:
        for start in range(0, len(receipts), SQS_MAX_BATCH):
            batch = receipts[start : start + SQS_MAX_BATCH]
            self._sqs.change_message_visibility_batch(
                QueueUrl=self._dlq_url,
                Entries=[
                    {"Id": str(i), "ReceiptHandle": handle, "VisibilityTimeout": 0}
                    for i, handle in enumerate(batch)
                ],
            )


def _describe(message: MessageTypeDef) -> dict[str, Any]:
    attributes = message.get("Attributes", {})
    described: dict[str, Any] = {
        "messageId": message["MessageId"],
        "receiveCount": int(attributes.get("ApproximateReceiveCount", 0)),
        # First receive from the main queue = the first (failed) processing attempt.
        "firstFailureAt": _timestamp(attributes.get("ApproximateFirstReceiveTimestamp")),
        "sentAt": _timestamp(attributes.get("SentTimestamp")),
        "objects": [],
    }
    try:
        body = json.loads(message["Body"])
        for record in body.get("Records", []):
            obj = record["s3"]["object"]
            described["objects"].append(
                {
                    "bucket": record["s3"]["bucket"]["name"],
                    "key": unquote_plus(obj["key"]),
                    "versionId": obj.get("versionId"),
                    "eTag": obj.get("eTag"),
                }
            )
    except (ValueError, KeyError, TypeError, AttributeError):
        described["unparseableBody"] = True
    return described


def _timestamp(epoch_ms: str | None) -> str | None:
    if not epoch_ms:
        return None
    return iso_ms(datetime.fromtimestamp(int(epoch_ms) / 1000, tz=UTC))


def _task(task: ListMessageMoveTasksResultEntryTypeDef) -> dict[str, Any]:
    keys = (
        "TaskHandle",
        "Status",
        "ApproximateNumberOfMessagesMoved",
        "ApproximateNumberOfMessagesToMove",
        "MaxNumberOfMessagesPerSecond",
        "FailureReason",
        "StartedTimestamp",
    )
    raw: dict[str, Any] = dict(task)
    result = {key[0].lower() + key[1:]: raw[key] for key in keys if key in raw}
    if "startedTimestamp" in result:
        result["startedAt"] = _timestamp(str(result.pop("startedTimestamp")))
    return result

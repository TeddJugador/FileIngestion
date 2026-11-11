from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from ingestion.replay import REQUEST_ADAPTER, DlqOperations, InspectRequest
from tests.unit.aws import REGION, aws_environment

DLQ_ARN = "arn:aws:sqs:us-east-1:123456789012:file-processing-dlq"
MAIN_ARN = "arn:aws:sqs:us-east-1:123456789012:file-processing-queue"


def _s3_body(key: str, version: str = "v1") -> str:
    return json.dumps(
        {
            "Records": [
                {
                    "eventTime": "2026-11-11T08:00:00.000Z",
                    "s3": {
                        "bucket": {"name": "school-ingestion-x"},
                        "object": {"key": key, "size": 1, "eTag": "e1", "versionId": version},
                    },
                }
            ]
        }
    )


@pytest.fixture
def dlq(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str, DlqOperations]]:
    aws_environment(monkeypatch)
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        url = sqs.create_queue(QueueName="file-processing-dlq")["QueueUrl"]
        yield sqs, url, DlqOperations(sqs, dlq_url=url, dlq_arn=DLQ_ARN, main_queue_arn=MAIN_ARN)


# --- inspect ----------------------------------------------------------------------------


def test_inspect_describes_messages_without_deleting_them(
    dlq: tuple[Any, str, DlqOperations],
) -> None:
    sqs, url, ops = dlq
    sqs.send_message(QueueUrl=url, MessageBody=_s3_body("raw/marks/SCH-001/term+4.csv", "v9"))
    result = ops.inspect(10)

    assert result["returned"] == 1
    (message,) = result["messages"]
    assert message["objects"] == [
        {
            "bucket": "school-ingestion-x",
            "key": "raw/marks/SCH-001/term 4.csv",
            "versionId": "v9",
            "eTag": "e1",
        }
    ]
    assert message["receiveCount"] >= 1
    assert message["firstFailureAt"].endswith("Z")
    assert message["sentAt"].endswith("Z")

    # Still there, and immediately visible again.
    again = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
    assert len(again.get("Messages", [])) == 1


def test_inspect_respects_max_and_reports_depth(dlq: tuple[Any, str, DlqOperations]) -> None:
    sqs, url, ops = dlq
    for i in range(25):
        sqs.send_message(QueueUrl=url, MessageBody=_s3_body(f"raw/marks/SCH-001/f{i}.csv"))
    result = ops.inspect(12)
    assert result["returned"] == 12
    assert len({m["messageId"] for m in result["messages"]}) == 12
    assert result["approximateDepth"] == 25


def test_inspect_empty_dlq(dlq: tuple[Any, str, DlqOperations]) -> None:
    _, _, ops = dlq
    assert ops.inspect(10) == {
        "action": "inspect",
        "approximateDepth": 0,
        "returned": 0,
        "messages": [],
    }


def test_inspect_flags_unparseable_bodies(dlq: tuple[Any, str, DlqOperations]) -> None:
    sqs, url, ops = dlq
    sqs.send_message(QueueUrl=url, MessageBody="not json")
    (message,) = ops.inspect(1)["messages"]
    assert message["unparseableBody"] is True
    assert message["objects"] == []


# --- redrive / status (moto doesn't implement move tasks: stub the real API model) -----


def _stubbed() -> tuple[DlqOperations, Stubber]:
    client = boto3.client(
        "sqs", region_name=REGION, aws_access_key_id="x", aws_secret_access_key="x"
    )
    stubber = Stubber(client)
    return DlqOperations(
        client, dlq_url="https://q/dlq", dlq_arn=DLQ_ARN, main_queue_arn=MAIN_ARN
    ), stubber


def test_redrive_moves_dlq_back_to_main_queue() -> None:
    ops, stubber = _stubbed()
    stubber.add_response(
        "start_message_move_task",
        {"TaskHandle": "handle-1"},
        {"SourceArn": DLQ_ARN, "DestinationArn": MAIN_ARN, "MaxNumberOfMessagesPerSecond": 10},
    )
    with stubber:
        result = ops.redrive(10)
    stubber.assert_no_pending_responses()
    assert result["taskHandle"] == "handle-1"
    assert result["next"] == {"action": "status", "taskHandle": "handle-1"}


def test_status_matches_a_running_task_by_handle() -> None:
    ops, stubber = _stubbed()
    started = int(datetime(2026, 11, 11, 9, 0, tzinfo=UTC).timestamp() * 1000)
    stubber.add_response(
        "list_message_move_tasks",
        {
            "Results": [
                {
                    "TaskHandle": "handle-1",  # present only while RUNNING
                    "Status": "RUNNING",
                    "SourceArn": DLQ_ARN,
                    "DestinationArn": MAIN_ARN,
                    "ApproximateNumberOfMessagesMoved": 3,
                    "ApproximateNumberOfMessagesToMove": 5,
                    "MaxNumberOfMessagesPerSecond": 10,
                    "StartedTimestamp": started,
                },
            ]
        },
        {"SourceArn": DLQ_ARN, "MaxResults": 10},
    )
    with stubber:
        result = ops.status("handle-1")
    assert result["matchedHandle"] is True
    assert result["task"] == {
        "taskHandle": "handle-1",
        "status": "RUNNING",
        "approximateNumberOfMessagesMoved": 3,
        "approximateNumberOfMessagesToMove": 5,
        "maxNumberOfMessagesPerSecond": 10,
        "startedAt": "2026-11-11T09:00:00.000Z",
    }


# Shape seen on a real account: completed tasks carry no TaskHandle, newest first.
COMPLETED_TASKS = {
    "Results": [
        {
            "Status": "COMPLETED",
            "SourceArn": DLQ_ARN,
            "ApproximateNumberOfMessagesMoved": 1,
            "ApproximateNumberOfMessagesToMove": 1,
            "MaxNumberOfMessagesPerSecond": 10,
            "StartedTimestamp": 1794417406969,
        },
        {"Status": "COMPLETED", "SourceArn": DLQ_ARN, "StartedTimestamp": 1794417372712},
    ]
}


def test_status_of_a_finished_task_shows_the_latest_with_a_note() -> None:
    ops, stubber = _stubbed()
    stubber.add_response(
        "list_message_move_tasks", COMPLETED_TASKS, {"SourceArn": DLQ_ARN, "MaxResults": 10}
    )
    with stubber:
        result = ops.status("eyJ0YXNr-the-handle-redrive-returned")
    assert result["matchedHandle"] is False
    assert result["task"]["status"] == "COMPLETED"
    assert result["task"]["approximateNumberOfMessagesMoved"] == 1
    assert result["task"]["startedAt"] == "2026-11-11T17:16:46.969Z"  # the newest
    assert "RUNNING" in result["note"]


def test_status_without_a_handle_shows_the_latest_task() -> None:
    ops, stubber = _stubbed()
    stubber.add_response(
        "list_message_move_tasks", COMPLETED_TASKS, {"SourceArn": DLQ_ARN, "MaxResults": 10}
    )
    with stubber:
        result = ops.status()
    assert result["task"]["startedAt"] == "2026-11-11T17:16:46.969Z"
    assert "note" not in result


def test_status_with_no_tasks() -> None:
    ops, stubber = _stubbed()
    stubber.add_response(
        "list_message_move_tasks", {"Results": []}, {"SourceArn": DLQ_ARN, "MaxResults": 10}
    )
    with stubber:
        assert ops.status() == {"action": "status", "matchedHandle": False, "task": None}


# --- request validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "inspect"},
        {"action": "inspect", "max": 100},
        {"action": "redrive", "maxPerSecond": 10},
        {"action": "status", "taskHandle": "h"},
        {"action": "status"},
    ],
)
def test_valid_requests(payload: dict[str, Any]) -> None:
    REQUEST_ADAPTER.validate_python(payload)


def test_inspect_defaults_to_ten() -> None:
    request = REQUEST_ADAPTER.validate_python({"action": "inspect"})
    assert isinstance(request, InspectRequest)
    assert request.max == 10


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "delete"},
        {"action": "inspect", "max": 0},
        {"action": "inspect", "max": 101},
        {"action": "redrive", "maxPerSecond": 501},
        {"action": "status", "taskHandle": ""},
        {"action": "inspect", "unexpected": True},
    ],
)
def test_handler_rejects_bad_payloads_with_guidance(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    aws_environment(monkeypatch)
    from ingestion.handlers import dlq_replay
    from tests.unit.test_handler import FakeContext

    with pytest.raises(dlq_replay.InvalidRequestError, match="Expected one of"):
        dlq_replay.handler(payload, FakeContext())


def test_handler_end_to_end(
    dlq: tuple[Any, str, DlqOperations], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingestion.handlers import dlq_replay
    from tests.unit.test_handler import FakeContext

    sqs, url, _ = dlq
    sqs.send_message(QueueUrl=url, MessageBody=_s3_body("raw/marks/SCH-001/t.csv"))
    monkeypatch.setenv("DLQ_URL", url)
    monkeypatch.setenv("DLQ_ARN", DLQ_ARN)
    monkeypatch.setenv("MAIN_QUEUE_ARN", MAIN_ARN)
    dlq_replay.operations.cache_clear()
    try:
        result = dlq_replay.handler({"action": "inspect", "max": 5}, FakeContext())
    finally:
        dlq_replay.operations.cache_clear()
    assert result["returned"] == 1

"""Capture events from SchoolEventBus in integration tests.

A temporary rule delivers matching events to a temporary SQS queue. New rules take a
few seconds to start delivering, and the pipeline can finish faster than that (an
indexing run takes ~3 s), so ``EventCapture`` proves the rule is live first: it sends a
canary event the rule also matches and waits until it arrives. Only then does the test
upload anything.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import boto3
import pytest

BUS = "SchoolEventBus"
CANARY_SOURCE = "integration.test.canary"
RULE_READY_TIMEOUT_SECONDS = 90


@dataclass
class EventCapture:
    sqs: Any
    queue_url: str

    def wait_for(
        self, matches: Callable[[dict[str, Any]], bool], *, timeout: float, what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.sqs.receive_message(
                QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10
            )
            for message in response.get("Messages", []):
                event = json.loads(message["Body"])
                if event.get("source") != CANARY_SOURCE and matches(event):
                    return dict(event)
        pytest.fail(f"no event for {what} after {timeout:.0f}s: check the service's logs")


@contextmanager
def capture_events(*, source: str, school_id: str) -> Iterator[EventCapture]:
    events, sqs = boto3.client("events"), boto3.client("sqs")
    name = f"ingestion-it-{uuid.uuid4().hex[:8]}"  # prefix the OIDC role is scoped to
    queue_url = sqs.create_queue(QueueName=name)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    rule_arn = events.put_rule(
        Name=name,
        EventBusName=BUS,
        EventPattern=json.dumps(
            {
                "source": [source, CANARY_SOURCE],
                "detail": {"metadata": {"schoolId": [school_id]}},
            }
        ),
    )["RuleArn"]
    sqs.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={
            "Policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "events.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": queue_arn,
                            "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
                        }
                    ],
                }
            )
        },
    )
    events.put_targets(Rule=name, EventBusName=BUS, Targets=[{"Id": "capture", "Arn": queue_arn}])
    try:
        _wait_until_rule_delivers(events, sqs, queue_url, school_id)
        yield EventCapture(sqs=sqs, queue_url=queue_url)
    finally:
        events.remove_targets(Rule=name, EventBusName=BUS, Ids=["capture"])
        events.delete_rule(Name=name, EventBusName=BUS)
        sqs.delete_queue(QueueUrl=queue_url)


def _wait_until_rule_delivers(events: Any, sqs: Any, queue_url: str, school_id: str) -> None:
    canary_id = uuid.uuid4().hex
    deadline = time.monotonic() + RULE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        events.put_events(
            Entries=[
                {
                    "Source": CANARY_SOURCE,
                    "DetailType": "Canary",
                    "Detail": json.dumps(
                        {"metadata": {"schoolId": school_id}, "data": {"canary": canary_id}}
                    ),
                    "EventBusName": BUS,
                }
            ]
        )
        response = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=5
        )
        for message in response.get("Messages", []):
            body = json.loads(message["Body"])
            if body.get("detail", {}).get("data", {}).get("canary") == canary_id:
                return
    pytest.fail("the temporary EventBridge rule never started delivering")

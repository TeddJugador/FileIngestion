"""Opt-in end-to-end test against the deployed stacks: ``pytest -m integration``.

Uploads fixtures with the real ``uploaded-by`` metadata, polls for ``summary.json``,
and captures the published event through a temporary EventBridge rule -> SQS queue.
It is also the check that the arm64 dependency layer imports on Lambda.

Uses school ``SCH-999`` so test traffic is easy to tell apart. Staged files expire with
the bucket's lifecycle rules; the temporary rule and queue are deleted afterwards.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.integration.capture import EventCapture, capture_events
from tests.unit.conftest import fixture_bytes

pytestmark = pytest.mark.integration

STACK = "SchoolIngestionStack"
SCHOOL = "SCH-999"
UPLOADER = "USR-integration-test"
TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class Deployed:
    bucket: str
    s3: Any
    capture: EventCapture


def _outputs(cloudformation: Any) -> dict[str, str]:
    stack = cloudformation.describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


@pytest.fixture(scope="module")
def deployed() -> Iterator[Deployed]:
    session = boto3.session.Session()
    try:
        outputs = _outputs(session.client("cloudformation"))
    except ClientError as exc:
        pytest.fail(f"{STACK} is not deployed (or no access): {exc}")
    with capture_events(source="school.ingestion", school_id=SCHOOL) as capture:
        yield Deployed(bucket=outputs["BucketName"], s3=session.client("s3"), capture=capture)


def _upload(deployed: Deployed, dataset: str, name: str, body: bytes) -> tuple[str, str]:
    key = f"raw/{dataset}/{SCHOOL}/{uuid.uuid4().hex[:8]}-{name}"
    version = deployed.s3.put_object(
        Bucket=deployed.bucket, Key=key, Body=body, Metadata={"uploaded-by": UPLOADER}
    )["VersionId"]
    digest = hashlib.sha256(f"{deployed.bucket}/{key}/{version}".encode()).hexdigest()
    return key, f"imp-{digest[:12]}"


def _wait_for_json(deployed: Deployed, key: str) -> Any:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            body = deployed.s3.get_object(Bucket=deployed.bucket, Key=key)["Body"].read()
            return json.loads(body)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("NoSuchKey", "AccessDenied", "404", "403"):
                raise
        time.sleep(3)
    pytest.fail(f"no s3://{deployed.bucket}/{key} after {TIMEOUT_SECONDS}s: check the worker logs")


def _wait_for_event(deployed: Deployed, import_id: str) -> dict[str, Any]:
    return deployed.capture.wait_for(
        lambda e: e["detail"]["data"].get("importId") == import_id,
        timeout=TIMEOUT_SECONDS,
        what=import_id,
    )


def test_valid_marks_file_is_imported_and_announced(deployed: Deployed) -> None:
    key, import_id = _upload(deployed, "marks", "term4.csv", fixture_bytes("marks_valid.csv"))
    prefix = f"processed/marks/{SCHOOL}/{import_id}"

    summary = _wait_for_json(deployed, f"{prefix}/summary.json")
    assert summary["status"] == "COMPLETED"
    assert (summary["totalRows"], summary["validRows"], summary["invalidRows"]) == (3, 3, 0)

    event = _wait_for_event(deployed, import_id)
    assert event["source"] == "school.ingestion"
    assert event["detail-type"] == "ImportCompleted"
    data = event["detail"]["data"]
    assert data["sourceFileKey"] == key
    assert data["validRecordsKey"] == f"{prefix}/valid.jsonl"
    assert data["uploadedBy"] == UPLOADER
    assert event["detail"]["metadata"]["correlationId"] == import_id

    lines = (
        deployed.s3.get_object(Bucket=deployed.bucket, Key=data["validRecordsKey"])["Body"]
        .read()
        .splitlines()
    )
    assert len(lines) == 3


def test_bad_header_is_reported_as_import_failed(deployed: Deployed) -> None:
    _, import_id = _upload(deployed, "marks", "bad.csv", fixture_bytes("marks_header_mismatch.csv"))
    summary = _wait_for_json(deployed, f"processed/marks/{SCHOOL}/{import_id}/summary.json")
    assert summary["status"] == "FAILED"
    assert summary["reasonCode"] == "HEADER_MISMATCH"

    event = _wait_for_event(deployed, import_id)
    assert event["detail-type"] == "ImportFailed"
    assert event["detail"]["data"]["reasonCode"] == "HEADER_MISMATCH"

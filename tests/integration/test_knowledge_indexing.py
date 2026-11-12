"""Opt-in end-to-end indexing test against the deployed stacks (``pytest -m integration``).

Uploads a Markdown policy through the ingestion bucket with the metadata ingestion
requires, waits for ``DocumentIndexed`` from the knowledge service, then reads one vector
back from S3 Vectors. Needs the Gemini key in Secrets Manager (scripts/set_provider_key.sh)
and costs a fraction of a cent. Uses school SCH-999, like the ingestion test.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.integration.capture import EventCapture, capture_events

pytestmark = pytest.mark.integration

SCHOOL = "SCH-999"
TIMEOUT_SECONDS = 300

POLICY = """# Transport Rules

## Morning buses

Buses leave the Market Square stop at 06:45 and arrive at school by 07:20.
Learners must be at the stop five minutes early. The driver will not wait.

## Behaviour on the bus

Learners stay seated with seatbelts fastened for the whole journey.
Eating on the bus is not allowed. Repeated misbehaviour leads to suspension
from the bus service for one week, and parents are informed in writing.
"""


@dataclass(frozen=True)
class Deployed:
    s3: Any
    s3vectors: Any
    capture: EventCapture
    ingestion_bucket: str
    vector_bucket: str
    index_name: str


def _outputs(stack: str) -> dict[str, str]:
    description = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


@pytest.fixture(scope="module")
def deployed() -> Iterator[Deployed]:
    try:
        ingestion = _outputs("SchoolIngestionStack")
        knowledge = _outputs("SchoolKnowledgeStack")
        boto3.client("secretsmanager").describe_secret(SecretId="knowledge/gemini")
    except ClientError as exc:
        pytest.skip(f"knowledge stack or Gemini key not available: {exc.response['Error']['Code']}")
    with capture_events(source="school.knowledge", school_id=SCHOOL) as capture:
        yield Deployed(
            s3=boto3.client("s3"),
            s3vectors=boto3.client("s3vectors"),
            capture=capture,
            ingestion_bucket=ingestion["BucketName"],
            vector_bucket=knowledge["VectorBucketName"],
            index_name=knowledge["VectorIndexName"],
        )


def test_uploaded_document_is_indexed(deployed: Deployed) -> None:
    # A unique path per run, so each run is a new document.
    relative_path = f"it/{uuid.uuid4().hex[:8]}/transport.md"
    body = POLICY.encode()
    deployed.s3.put_object(
        Bucket=deployed.ingestion_bucket,
        Key=f"raw/documents/{SCHOOL}/{relative_path}",
        Body=body,
        Metadata={
            "uploaded-by": "USR-integration-test",
            "audience": "PARENT",
            "title": "Transport Rules",
        },
    )
    version = f"v-{hashlib.sha256(body).hexdigest()[:12]}"
    document_id = (
        f"doc-{hashlib.sha256(f'{SCHOOL}/{relative_path.lower()}'.encode()).hexdigest()[:12]}"
    )

    # Source of truth first: the document's first vector exists in S3 Vectors.
    key = f"{document_id}#{version}#00000"
    vector = _wait_for_vector(deployed, key)
    assert vector["metadata"]["school_id"] == SCHOOL
    assert vector["metadata"]["audience"] == ["PARENT"]
    assert "Market Square" in vector["metadata"]["chunk_text"]

    # Then the announcement. Indexing is already proven above, so a failure here means
    # only the event delivery (or its capture) went wrong.
    event = deployed.capture.wait_for(
        lambda e: (
            e["detail"]["data"].get("version") == version
            and e["detail"]["data"].get("documentId") == document_id
        ),
        timeout=120,
        what=f"DocumentIndexed for {document_id} (vectors already stored)",
    )
    assert event["detail-type"] == "DocumentIndexed", event["detail"]["data"]
    data = event["detail"]["data"]
    assert data["documentId"] == document_id
    assert data["replacedVersion"] is None
    assert data["chunkCount"] >= 1
    assert data["indexName"] == deployed.index_name
    assert data["audience"] == ["PARENT"]


def _wait_for_vector(deployed: Deployed, key: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        found = deployed.s3vectors.get_vectors(
            vectorBucketName=deployed.vector_bucket,
            indexName=deployed.index_name,
            keys=[key],
            returnMetadata=True,
        )["vectors"]
        if found:
            return dict(found[0])
        time.sleep(3)
    pytest.fail(f"vector {key} not stored after {TIMEOUT_SECONDS}s: check the indexer logs")

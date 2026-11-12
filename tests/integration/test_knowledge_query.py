"""Opt-in end-to-end test of the query API (``pytest -m integration``).

Creates a temporary parent and admin in Cognito, indexes a parent document and a
staff-only document through ingestion, then asks questions over HTTP as the parent:
the answer must cite the parent document and never reveal the staff-only fact. Also
checks the cosine-distance convention against real S3 Vectors, and retires both
documents through the admin route. Needs the Gemini and Anthropic keys; a fraction of
a cent per run. Everything uses school SCH-999.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.integration.capture import EventCapture, capture_events

pytestmark = pytest.mark.integration

SCHOOL = "SCH-999"
CONTEXT = json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text())["context"]
# The key the deployed generator actually uses (Claude, or Gemini for both roles).
GENERATION_SECRET = (
    "knowledge/anthropic" if CONTEXT["generationProvider"] == "CLAUDE" else "knowledge/gemini"
)
TIMEOUT_SECONDS = 300

PARENT_DOC = """# Aftercare

## Hours and late collection

Aftercare runs in the school hall and closes at 17:30 on weekdays. Children must be
collected by then. Late collection costs R50 for every quarter hour after 17:30.
"""

STAFF_DOC = """# Staff Parking

## Access codes

The staff parking gate code changes every term. This term the gate code is 4471.
Do not share the code with parents or visitors.
"""


def _outputs(stack: str) -> dict[str, str]:
    description = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


@dataclass(frozen=True)
class Api:
    url: str
    parent_token: str
    admin_token: str
    capture: EventCapture
    ingestion_bucket: str
    vector_bucket: str
    index_name: str

    def call(self, method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
        request = urllib.request.Request(
            self.url.rstrip("/") + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


def _create_user(cognito: Any, pool_id: str, client_id: str, group: str) -> tuple[str, str]:
    email = f"it-{group}-{uuid.uuid4().hex[:8]}@example.com"
    password = f"It{secrets.token_urlsafe(12)}!7a"
    cognito.admin_create_user(
        UserPoolId=pool_id,
        Username=email,
        MessageAction="SUPPRESS",
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:school_id", "Value": SCHOOL},
        ],
    )
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=email, Password=password, Permanent=True
    )
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    tokens = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )["AuthenticationResult"]
    return email, tokens["IdToken"]  # ID token: only it carries custom:school_id


@pytest.fixture(scope="module")
def api() -> Iterator[Api]:
    try:
        shared, ingestion, knowledge = (
            _outputs("SchoolSharedStack"),
            _outputs("SchoolIngestionStack"),
            _outputs("SchoolKnowledgeStack"),
        )
        for secret in {"knowledge/gemini", GENERATION_SECRET}:
            boto3.client("secretsmanager").describe_secret(SecretId=secret)
    except ClientError as exc:
        pytest.skip(f"stacks or provider keys not available: {exc.response['Error']['Code']}")
    if "TestClientId" not in shared:
        pytest.skip("no password test client (not the dev stage)")

    cognito = boto3.client("cognito-idp")
    pool_id, client_id = shared["UserPoolId"], shared["TestClientId"]
    users: list[str] = []
    try:
        parent_email, parent_token = _create_user(cognito, pool_id, client_id, "parent")
        users.append(parent_email)
        admin_email, admin_token = _create_user(cognito, pool_id, client_id, "admin")
        users.append(admin_email)
    except ClientError as exc:
        for user in users:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=user)
        pytest.skip(f"cannot create test users: {exc.response['Error']['Code']}")
    try:
        with capture_events(source="school.knowledge", school_id=SCHOOL) as capture:
            yield Api(
                url=knowledge["ApiUrl"],
                parent_token=parent_token,
                admin_token=admin_token,
                capture=capture,
                ingestion_bucket=ingestion["BucketName"],
                vector_bucket=knowledge["VectorBucketName"],
                index_name=knowledge["VectorIndexName"],
            )
    finally:
        for user in users:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=user)


def _upload_and_wait(api: Api, text: str, audience: str, title: str) -> str:
    relative_path = f"it/{uuid.uuid4().hex[:8]}/{title.lower().replace(' ', '-')}.md"
    body = text.encode()
    boto3.client("s3").put_object(
        Bucket=api.ingestion_bucket,
        Key=f"raw/documents/{SCHOOL}/{relative_path}",
        Body=body,
        Metadata={"uploaded-by": "USR-integration-test", "audience": audience, "title": title},
    )
    document_id = (
        f"doc-{hashlib.sha256(f'{SCHOOL}/{relative_path.lower()}'.encode()).hexdigest()[:12]}"
    )
    event = api.capture.wait_for(
        lambda e: e["detail"]["data"].get("documentId") == document_id,
        timeout=TIMEOUT_SECONDS,
        what=f"indexing of {title}",
    )
    assert event["detail-type"] == "DocumentIndexed", event["detail"]["data"]
    return document_id


def test_parent_question_answered_with_citations_and_no_staff_leak(api: Api) -> None:
    parent_doc = _upload_and_wait(api, PARENT_DOC, "PARENT", "Aftercare")
    staff_doc = _upload_and_wait(api, STAFF_DOC, "STAFF", "Staff Parking")

    status, answer = api.call(
        "POST",
        "/ask",
        api.parent_token,
        {"question": "What time does aftercare close, and what does late collection cost?"},
    )
    assert status == 200, answer
    assert answer["outcome"] == "ANSWERED"
    assert answer["grounded"] is True
    cited = {c["documentId"] for c in answer["citations"]}
    assert parent_doc in cited
    assert staff_doc not in cited
    assert "17:30" in answer["answer"]

    # The staff-only fact must not reach a parent, however directly they ask.
    status, leak = api.call(
        "POST", "/ask", api.parent_token, {"question": "What is the staff parking gate code?"}
    )
    assert status == 200, leak
    assert "4471" not in leak["answer"]
    assert staff_doc not in {c["documentId"] for c in leak["citations"]}

    # Feedback on one's own answer is accepted.
    status, _ = api.call(
        "POST", "/feedback", api.parent_token, {"queryId": answer["queryId"], "helpful": True}
    )
    assert status == 200

    # Parents can't use admin routes; the admin can list and retire.
    assert api.call("GET", "/documents", api.parent_token)[0] == 403
    status, listing = api.call("GET", "/documents", api.admin_token)
    assert status == 200
    assert {parent_doc, staff_doc} <= {d["documentId"] for d in listing["documents"]}
    for document_id in (parent_doc, staff_doc):
        status, retired = api.call("POST", f"/documents/{document_id}/retire", api.admin_token)
        assert (status, retired["status"]) == (200, "RETIRED")


def test_requests_without_a_token_are_rejected(api: Api) -> None:
    request = urllib.request.Request(
        api.url.rstrip("/") + "/ask", method="POST", data=b'{"question": "When are fees due?"}'
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=30)
    assert excinfo.value.code == 401


def test_cosine_distance_convention(api: Api) -> None:
    """The query path treats ``distance`` as 1 - cosine similarity. Query with a stored
    vector altered (half its components zeroed), then check the returned distance
    against the cosine similarity computed locally from the hit's own vector."""
    s3vectors = boto3.client("s3vectors")
    listed = _first_vectors(s3vectors, api)
    if not listed:
        pytest.skip("index is empty")
    stored = listed[0]["data"]["float32"]
    query = [v if i % 2 == 0 else 0.0 for i, v in enumerate(stored)]

    (hit,) = s3vectors.query_vectors(
        vectorBucketName=api.vector_bucket,
        indexName=api.index_name,
        queryVector={"float32": query},
        topK=1,
        returnDistance=True,
    )["vectors"]
    (fetched,) = s3vectors.get_vectors(
        vectorBucketName=api.vector_bucket,
        indexName=api.index_name,
        keys=[hit["key"]],
        returnData=True,
    )["vectors"]
    hit_vector = fetched["data"]["float32"]

    dot = sum(a * b for a, b in zip(query, hit_vector, strict=True))
    norms = math.sqrt(sum(a * a for a in query)) * math.sqrt(sum(b * b for b in hit_vector))
    similarity = dot / norms
    assert 0.2 < similarity < 0.99  # a genuinely partial match, not a trivial case
    assert hit["distance"] == pytest.approx(1 - similarity, abs=0.01)


def _first_vectors(s3vectors: Any, api: Api) -> list[dict[str, Any]]:
    """S3 Vectors can return an empty page with a nextToken; keep paging."""
    kwargs: dict[str, Any] = {}
    for _ in range(50):
        page = s3vectors.list_vectors(
            vectorBucketName=api.vector_bucket,
            indexName=api.index_name,
            maxResults=10,
            returnData=True,
            **kwargs,
        )
        if page.get("vectors"):
            return list(page["vectors"])
        if not page.get("nextToken"):
            return []
        kwargs = {"nextToken": page["nextToken"]}
    return []

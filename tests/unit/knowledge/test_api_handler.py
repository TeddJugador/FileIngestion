"""The HTTP layer: routing, validation, identity from the token, status codes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest

from knowledge.providers.base import GenerationRequest, GenerationResult
from knowledge.providers.errors import ProviderRetryableError
from knowledge.providers.fakes import FakeGenerator
from tests.unit.knowledge.indexing_harness import KnowledgeHarness
from tests.unit.knowledge.query_harness import create_query_log, index_text, query_service
from tests.unit.test_handler import FakeContext

FEES = "# Fee Policy\n\n## Due dates\n\nSchool fees are due on the seventh day of every month."


def http_event(
    method: str,
    path: str,
    body: Any = None,
    *,
    groups: str = "[parent]",
    school: str = "SCH-001",
    sub: str = "user-1",
    raw_body: str | None = None,
) -> dict[str, Any]:
    route = "/documents/{documentId}/retire" if path.endswith("/retire") else path
    return {
        "version": "2.0",
        "routeKey": f"{method} {route}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "api",
            "domainName": "api.example",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.1",
                "userAgent": "test",
            },
            "requestId": "req-1",
            "routeKey": f"{method} {route}",
            "stage": "$default",
            "time": "12/Nov/2026:08:00:00 +0000",
            "timeEpoch": 1794470400000,
            "authorizer": {
                "jwt": {
                    "claims": {"sub": sub, "custom:school_id": school, "cognito:groups": groups},
                    "scopes": None,
                }
            },
        },
        "pathParameters": {"documentId": path.split("/")[2]} if path.endswith("/retire") else None,
        "body": raw_body
        if raw_body is not None
        else (json.dumps(body) if body is not None else None),
        "isBase64Encoded": False,
    }


@pytest.fixture
def api(kh: KnowledgeHarness) -> Iterator[Any]:
    create_query_log()
    index_text(kh, FEES, document_id="doc-aaaaaaaaaaa1", audience=("PARENT",), title="Fee Policy")
    from knowledge.handlers import api as module

    with mock.patch.object(module, "service", return_value=query_service(kh)):
        yield module


def call(module: Any, event: dict[str, Any]) -> tuple[int, Any]:
    response = module.handler(event, FakeContext())
    return response["statusCode"], json.loads(response["body"]) if response.get("body") else None


def test_ask_returns_a_grounded_answer(api: Any) -> None:
    status, body = call(api, http_event("POST", "/ask", {"question": "When are school fees due?"}))
    assert status == 200
    assert body["outcome"] == "ANSWERED"
    assert body["grounded"] is True
    assert body["queryId"].startswith("qry-")
    assert body["citations"][0]["documentId"] == "doc-aaaaaaaaaaa1"


@pytest.mark.parametrize(
    "body",
    [
        {"question": "hi"},  # under 3 characters after trimming
        {"question": "   a  "},
        {"question": "x" * 1001},
        {"question": "When are fees due?", "topK": 11},
        {"question": "When are fees due?", "topK": 0},
        {"question": "When are fees due?", "topK": "5"},
        {"question": "When are fees due?", "school_id": "SCH-002"},  # never from the body
        {"question": "When are fees due?", "role": "admin"},
        {},
    ],
)
def test_invalid_ask_requests_are_400(api: Any, body: dict[str, Any]) -> None:
    status, response = call(api, http_event("POST", "/ask", body))
    assert status == 400
    assert "x" * 50 not in json.dumps(response)  # input isn't echoed back


def test_invalid_json_is_400(api: Any) -> None:
    status, _ = call(api, http_event("POST", "/ask", raw_body="{not json"))
    assert status == 400


@pytest.mark.parametrize(("groups", "school"), [("[teachers]", "SCH-001"), ("[parent]", "")])
def test_token_without_school_or_group_is_403(api: Any, groups: str, school: str) -> None:
    status, _ = call(
        api,
        http_event(
            "POST", "/ask", {"question": "When are fees due?"}, groups=groups, school=school
        ),
    )
    assert status == 403


def test_provider_error_is_503_with_the_query_id(api: Any, kh: KnowledgeHarness) -> None:
    class Failing(FakeGenerator):
        def generate(self, request: GenerationRequest) -> GenerationResult:
            raise ProviderRetryableError("claude", "overloaded", status_code=529)

    with mock.patch.object(api, "service", return_value=query_service(kh, generator=Failing())):
        status, body = call(
            api, http_event("POST", "/ask", {"question": "When are school fees due?"})
        )
    assert status == 503
    assert body["outcome"] == "PROVIDER_ERROR"
    assert body["queryId"].startswith("qry-")


def test_feedback_flow(api: Any) -> None:
    _, answer = call(
        api, http_event("POST", "/ask", {"question": "When are school fees due?"}, sub="parent-a")
    )
    ok, body = call(
        api,
        http_event(
            "POST", "/feedback", {"queryId": answer["queryId"], "helpful": True}, sub="parent-a"
        ),
    )
    assert (ok, body) == (200, {"queryId": answer["queryId"], "recorded": True})
    other, _ = call(
        api,
        http_event(
            "POST", "/feedback", {"queryId": answer["queryId"], "helpful": True}, sub="parent-b"
        ),
    )
    assert other == 404  # someone else's query looks like no query at all


@pytest.mark.parametrize(
    "body",
    [
        {"queryId": "qry-01JD0000000000000000000000", "helpful": "yes"},
        {"queryId": "not-a-query", "helpful": True},
        {"queryId": "qry-01JD0000000000000000000000", "helpful": True, "reasonCode": "RUDE"},
    ],
)
def test_invalid_feedback_is_400(api: Any, body: dict[str, Any]) -> None:
    status, _ = call(api, http_event("POST", "/feedback", body))
    assert status == 400


@pytest.mark.parametrize("groups", ["[parent]", "[staff]"])
def test_document_routes_are_admin_only(api: Any, groups: str) -> None:
    assert call(api, http_event("GET", "/documents", groups=groups))[0] == 403
    assert (
        call(api, http_event("POST", "/documents/doc-aaaaaaaaaaa1/retire", groups=groups))[0] == 403
    )


def test_admin_lists_and_retires(api: Any) -> None:
    status, body = call(api, http_event("GET", "/documents", groups="[admin]"))
    assert status == 200
    assert [d["documentId"] for d in body["documents"]] == ["doc-aaaaaaaaaaa1"]
    status, body = call(
        api, http_event("POST", "/documents/doc-aaaaaaaaaaa1/retire", groups="[admin]")
    )
    assert (status, body["status"]) == (200, "RETIRED")
    assert (
        call(api, http_event("POST", "/documents/doc-aaaaaaaaaaa1/retire", groups="[admin]"))[0]
        == 404
    )


def test_admins_only_see_their_own_school(api: Any) -> None:
    status, body = call(api, http_event("GET", "/documents", groups="[admin]", school="SCH-002"))
    assert (status, body["documents"]) == (200, [])

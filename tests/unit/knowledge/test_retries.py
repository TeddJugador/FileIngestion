from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from knowledge.providers.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderRetryableError,
)
from knowledge.providers.gemini import gemini_error_code
from knowledge.providers.http import INDEXING_RETRIES, QUERY_RETRIES, parse_retry_after
from tests.unit.knowledge.http_stub import Stub, ok, status

URL = "https://provider.example/v1/thing"


def test_success_needs_one_attempt() -> None:
    stub = Stub([ok({"ok": True})])
    assert stub.caller().post(URL, headers={}, body={}) == {"ok": True}
    assert len(stub.requests) == 1
    assert stub.sleeps == []


def test_429_is_retried_and_retry_after_is_honoured() -> None:
    stub = Stub([status(429, headers={"retry-after": "3"}), ok({"ok": True})])
    assert stub.caller().post(URL, headers={}, body={}) == {"ok": True}
    assert stub.sleeps == [3.0]  # longer than the 1 s backoff, so Retry-After wins


def test_5xx_retried_with_exponential_backoff_up_to_five_attempts_for_indexing() -> None:
    stub = Stub([status(503)])
    with pytest.raises(ProviderRetryableError) as excinfo:
        stub.caller(policy=INDEXING_RETRIES).post(URL, headers={}, body={})
    assert len(stub.requests) == 5
    assert stub.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert excinfo.value.status_code == 503
    assert excinfo.value.retryable is True
    assert stub.observer.errors == [("test", 503)]  # counted once, as one final failure


def test_queries_get_two_attempts() -> None:
    stub = Stub([status(500)])
    with pytest.raises(ProviderRetryableError):
        stub.caller(policy=QUERY_RETRIES).post(URL, headers={}, body={})
    assert len(stub.requests) == 2
    assert stub.sleeps == [0.5]


def test_long_retry_after_fails_fast_when_a_user_is_waiting() -> None:
    stub = Stub([status(429, headers={"retry-after": "120"})])
    with pytest.raises(ProviderRetryableError):
        stub.caller(policy=QUERY_RETRIES).post(URL, headers={}, body={})
    assert len(stub.requests) == 1
    assert stub.sleeps == []


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_other_4xx_raise_immediately(code: int) -> None:
    stub = Stub([status(code, {"error": {"status": "INVALID_ARGUMENT"}})])
    with pytest.raises(ProviderRequestError) as excinfo:
        stub.caller(error_code=gemini_error_code).post(URL, headers={}, body={})
    assert len(stub.requests) == 1
    assert stub.sleeps == []
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert stub.observer.errors == [("test", code)]


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "connect"]
)
def test_timeouts_and_connection_errors_are_retried(error: Exception) -> None:
    stub = Stub([error, ok({"ok": True})])
    assert stub.caller().post(URL, headers={}, body={}) == {"ok": True}
    assert len(stub.requests) == 2


def test_non_json_success_is_a_response_error() -> None:
    stub = Stub([httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(ProviderResponseError):
        stub.caller().post(URL, headers={}, body={})


def test_error_messages_never_contain_request_or_response_text() -> None:
    secret_question = "Does my son Thabo Ndlovu qualify for the fee waiver?"
    stub = Stub([status(400, {"error": {"status": "BAD", "message": secret_question}})])
    with pytest.raises(ProviderRequestError) as excinfo:
        stub.caller(error_code=gemini_error_code).post(URL, headers={}, body={"q": secret_question})
    assert "Thabo" not in str(excinfo.value)
    assert "fee waiver" not in str(excinfo.value)


def test_the_call_timeout_is_twenty_seconds() -> None:
    from knowledge.providers.http import JsonCaller

    caller = JsonCaller("test", INDEXING_RETRIES)
    assert caller._client.timeout == httpx.Timeout(20.0)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("7", 7.0),
        ("Wed, 11 Nov 2026 12:00:30 GMT", 30.0),
        ("Wed, 11 Nov 2026 11:59:00 GMT", 0.0),  # in the past
        ("soon", None),
        (None, None),
    ],
)
def test_parse_retry_after(header: str | None, seconds: float | None) -> None:
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after(header, now=now) == seconds

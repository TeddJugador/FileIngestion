"""One retry policy for every provider: backoff with full jitter on 429 and 5xx,
``Retry-After`` honoured, other 4xx raised immediately."""

from __future__ import annotations

import email.utils
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from knowledge.providers.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderRetryableError,
)

CALL_TIMEOUT_SECONDS = 20.0  # spec: 20 s per embedding or generation call
RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float
    max_delay: float
    # Longest single wait we'll accept. A Retry-After beyond this (say 60 s while a user
    # waits) fails fast instead of sleeping past the caller's own deadline.
    max_wait: float


# Spec: max 5 attempts for indexing, 2 for queries (a user is waiting).
INDEXING_RETRIES = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=30.0, max_wait=60.0)
QUERY_RETRIES = RetryPolicy(max_attempts=2, base_delay=0.5, max_delay=2.0, max_wait=2.0)


class ErrorObserver(Protocol):
    def provider_error(self, provider: str, status_code: int | None) -> None: ...


class _NoObserver:
    def provider_error(self, provider: str, status_code: int | None) -> None:
        pass


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


def _is_retryable(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


class JsonCaller:
    """POSTs JSON with retries. One instance per adapter; the client is reused across
    warm Lambda invocations."""

    def __init__(
        self,
        provider: str,
        policy: RetryPolicy,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        observer: ErrorObserver | None = None,
        error_code: Callable[[Any], str] = lambda _body: "",
    ) -> None:
        self.provider = provider
        self.policy = policy
        self._client = client or httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS))
        self._sleep = sleep
        self._jitter = jitter
        self._observer = observer or _NoObserver()
        self._error_code = error_code

    def post(self, url: str, *, headers: Mapping[str, str], body: Any) -> Any:  # noqa: ANN401
        last_status: int | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            retry_after: float | None = None
            try:
                response = self._client.post(url, headers=dict(headers), json=body)
            except httpx.TimeoutException:
                last_status = None
                reason = "timed out"
            except httpx.TransportError:
                last_status = None
                reason = "connection error"
            else:
                if response.is_success:
                    return self._json(response)
                last_status = response.status_code
                code = self._safe_error_code(response)
                if not _is_retryable(response.status_code):
                    self._observer.provider_error(self.provider, last_status)
                    raise ProviderRequestError(
                        self.provider, "request rejected", status_code=last_status, code=code
                    )
                reason = "throttled" if last_status == 429 else "server error"
                retry_after = parse_retry_after(response.headers.get("retry-after"))

            if attempt == self.policy.max_attempts:
                break
            delay = self._backoff(attempt, retry_after)
            if delay > self.policy.max_wait:
                break  # don't sleep past the caller's budget
            self._sleep(delay)

        self._observer.provider_error(self.provider, last_status)
        raise ProviderRetryableError(
            self.provider,
            f"{reason} after {attempt} attempt(s)",
            status_code=last_status,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        ceiling = min(self.policy.max_delay, self.policy.base_delay * 2.0 ** (attempt - 1))
        delay = ceiling * self._jitter()
        return max(delay, retry_after) if retry_after is not None else delay

    def _json(self, response: httpx.Response) -> Any:  # noqa: ANN401
        try:
            return response.json()
        except ValueError:
            raise ProviderResponseError(self.provider, "response was not JSON") from None

    def _safe_error_code(self, response: httpx.Response) -> str:
        """A short, content-free error code (e.g. INVALID_ARGUMENT), never the message."""
        try:
            return self._error_code(response.json())[:64]
        except Exception:
            return ""

"""A scripted fake HTTP layer for provider tests (no network, no keys)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from knowledge.providers.http import INDEXING_RETRIES, JsonCaller, RetryPolicy

Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class Recorder:
    errors: list[tuple[str, int | None]] = field(default_factory=list)

    def provider_error(self, provider: str, status_code: int | None) -> None:
        self.errors.append((provider, status_code))


@dataclass
class Stub:
    """Replies in order from ``script``; the last entry repeats."""

    script: list[Responder | httpx.Response | Exception]
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    observer: Recorder = field(default_factory=Recorder)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return item(request)

    def caller(
        self,
        provider: str = "test",
        policy: RetryPolicy = INDEXING_RETRIES,
        error_code: Callable[[Any], str] = lambda _b: "",
    ) -> JsonCaller:
        return JsonCaller(
            provider,
            policy,
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=self.sleeps.append,
            jitter=lambda: 1.0,  # deterministic: always the backoff ceiling
            observer=self.observer,
            error_code=error_code,
        )

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def status(code: int, payload: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(code, json=payload if payload is not None else {}, headers=headers)

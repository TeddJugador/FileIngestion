"""The alarms match metrics by exact dimensions. Check what the code really emits."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from infra.constructs.knowledge.alarms import provider_metric_names
from infra.constructs.knowledge.limits import API_SERVICE, INDEXER_SERVICE, METRICS_NAMESPACE
from infra.knowledge_config import KnowledgeConfig
from knowledge.metrics import PowertoolsMetricsSink


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]


def _dimension_sets(document: dict[str, object]) -> list[set[str]]:
    metrics = document["_aws"]["CloudWatchMetrics"]  # type: ignore[index]
    return [set(d) for block in metrics for d in block["Dimensions"]]


@pytest.mark.parametrize("service", [API_SERVICE, INDEXER_SERVICE])
def test_provider_errors_carry_service_and_provider(
    service: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", service)
    PowertoolsMetricsSink(METRICS_NAMESPACE).provider_error("gemini", 429)
    (document,) = _emitted(capsys)
    assert document["_aws"]["CloudWatchMetrics"][0]["Namespace"] == METRICS_NAMESPACE  # type: ignore[index]
    assert _dimension_sets(document) == [{"service", "Provider"}]
    assert (document["service"], document["Provider"], document["ProviderErrorCount"]) == (
        service,
        "gemini",
        [1.0],
    )


def test_model_mismatch_carries_only_the_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", API_SERVICE)
    PowertoolsMetricsSink(METRICS_NAMESPACE).record("EmbeddingModelMismatch", 1, "Count", {})
    (document,) = _emitted(capsys)
    assert _dimension_sets(document) == [{"service"}]
    assert document["service"] == API_SERVICE


@pytest.mark.parametrize(
    ("embedding", "generation", "expected"),
    [
        (
            "gemini",
            "CLAUDE",
            {(INDEXER_SERVICE, "gemini"), (API_SERVICE, "gemini"), (API_SERVICE, "claude")},
        ),
        ("gemini", "GEMINI", {(INDEXER_SERVICE, "gemini"), (API_SERVICE, "gemini")}),
        (
            "voyage",
            "GEMINI",
            {(INDEXER_SERVICE, "voyage"), (API_SERVICE, "voyage"), (API_SERVICE, "gemini")},
        ),
    ],
)
def test_alarm_covers_every_provider_in_use(
    embedding: str, generation: str, expected: set[tuple[str, str]]
) -> None:
    config = KnowledgeConfig(
        stage="dev",
        embedding_provider=embedding,
        embedding_model="m",
        embedding_dimension=1024,
        vector_index_version=1,
        generation_provider=generation,
        generation_model="g",
        generation_thinking="",
        generation_effort="low",
        cors_allowed_origins=("http://localhost:3000",),
    )
    assert set(provider_metric_names(config)) == expected


class _Recorder:
    def __init__(self) -> None:
        self.providers: list[str] = []

    def provider_error(self, provider: str, status_code: int | None) -> None:
        self.providers.append(provider)


@pytest.mark.parametrize(
    ("env", "role", "expected"),
    [
        (
            {
                "EMBEDDING_PROVIDER": "gemini",
                "EMBEDDING_MODEL": "gemini-embedding-2",
                "EMBEDDING_DIMENSION": "768",
            },
            "embed",
            "gemini",
        ),
        (
            {
                "EMBEDDING_PROVIDER": "voyage",
                "EMBEDDING_MODEL": "voyage-4",
                "EMBEDDING_DIMENSION": "1024",
            },
            "embed",
            "voyage",
        ),
        (
            {"GENERATION_PROVIDER": "CLAUDE", "GENERATION_MODEL": "claude-haiku-5-5"},
            "generate",
            "claude",
        ),
        (
            {"GENERATION_PROVIDER": "GEMINI", "GENERATION_MODEL": "gemini-3.5-flash-lite"},
            "generate",
            "gemini",
        ),
    ],
)
def test_failing_adapters_report_the_names_the_alarm_expects(
    env: dict[str, str], role: str, expected: str
) -> None:
    """Drive each real adapter through the factory, fail its HTTP call, and record the
    Provider name it reports: it must be one the alarm's metric list uses."""
    import httpx

    from knowledge.providers.base import EmbeddingDocument, GenerationRequest
    from knowledge.providers.errors import ProviderError
    from knowledge.providers.factory import ProviderSettings, build_embedder, build_generator
    from knowledge.providers.http import QUERY_RETRIES

    base = {
        "EMBEDDING_PROVIDER": "gemini",
        "EMBEDDING_MODEL": "gemini-embedding-2",
        "EMBEDDING_DIMENSION": "768",
        "GENERATION_PROVIDER": "CLAUDE",
        "GENERATION_MODEL": "claude-haiku-5-5",
    }
    settings = ProviderSettings.from_env({**base, **env})
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={})))
    recorder = _Recorder()
    kwargs = {
        "policy": QUERY_RETRIES,
        "key_loader": lambda _: "test-key",
        "observer": recorder,
        "client": client,
    }
    call: Callable[[], object]
    if role == "embed":
        embedder = build_embedder(settings, **kwargs)
        call = lambda: embedder.embed_documents([EmbeddingDocument(context="t", text="x")])  # noqa: E731
    else:
        generator = build_generator(settings, **kwargs)
        call = lambda: generator.generate(GenerationRequest(system="s", user="u"))  # noqa: E731
    with pytest.raises(ProviderError):
        call()
    assert recorder.providers == [expected]
    all_alarm_providers = {"gemini", "voyage", "claude"}
    assert expected in all_alarm_providers

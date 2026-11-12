from __future__ import annotations

import math

import pytest

from knowledge.providers import (
    EmbeddingDocument,
    EmbeddingModelMismatchError,
    GenerationRequest,
    check_embedding_model,
)
from knowledge.providers import secrets as secret_module
from knowledge.providers.claude import ClaudeGenerator
from knowledge.providers.factory import (
    ProviderSettings,
    build_embedder,
    build_generator,
    embedding_secret_name,
    generation_secret_name,
)
from knowledge.providers.fakes import FakeEmbedder, FakeGenerator
from knowledge.providers.gemini import GeminiEmbedder, GeminiGenerator
from knowledge.providers.http import INDEXING_RETRIES, QUERY_RETRIES
from knowledge.providers.secrets import MissingApiKeyError, parse_secret
from knowledge.providers.voyage import VoyageEmbedder
from tests.unit.knowledge.http_stub import Stub, ok

# --- fakes -------------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_fake_embedder_is_deterministic_and_normalised() -> None:
    embedder = FakeEmbedder()
    a = embedder.embed_query("school uniform rules")
    assert a == embedder.embed_query("school uniform rules")
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0)
    assert len(a) == embedder.dimension


def test_fake_embedder_ranks_overlapping_text_higher() -> None:
    embedder = FakeEmbedder()
    query = embedder.embed_query("when are school fees due")
    fees, uniform = embedder.embed_documents(
        [
            EmbeddingDocument("Fee Policy", "School fees are due on the first day of term."),
            EmbeddingDocument("Uniform Policy", "Learners wear navy blazers and black shoes."),
        ]
    )
    assert _cosine(query, fees) > _cosine(query, uniform)


def test_fake_generator_echoes_given_source_ids() -> None:
    generator = FakeGenerator()
    result = generator.generate(
        GenerationRequest(system="s", user='<source id="S1">a</source><source id="S3">b</source>')
    )
    assert "[S1]" in result.text
    assert "[S3]" in result.text
    assert generator.requests[0].user.startswith("<source")


def test_fake_generator_answer_override() -> None:
    assert (
        FakeGenerator(answer="Invented [S9].").generate(GenerationRequest(system="", user="")).text
        == "Invented [S9]."
    )


# --- model binding -----------------------------------------------------------------------


def test_model_mismatch_detected() -> None:
    check_embedding_model("gemini/gemini-embedding-2", "gemini/gemini-embedding-2")
    with pytest.raises(EmbeddingModelMismatchError):
        check_embedding_model("voyage/voyage-4", "gemini/gemini-embedding-2")


# --- secrets -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("  plain-key\n", "plain-key"),
        ('{"apiKey": "json-key"}', "json-key"),
        (b"bytes-key", "bytes-key"),
    ],
)
def test_parse_secret(raw: str | bytes, key: str) -> None:
    assert parse_secret(raw) == key


@pytest.mark.parametrize("raw", ["", "   ", '{"other": "x"}', "{not json"])
def test_empty_or_malformed_secret_is_rejected(raw: str) -> None:
    with pytest.raises(MissingApiKeyError):
        parse_secret(raw)


def test_load_api_key_caches_for_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_get_secret(name: str, max_age: int) -> str:
        calls.append((name, max_age))
        return "k"

    monkeypatch.setattr(secret_module.parameters, "get_secret", fake_get_secret)
    assert secret_module.load_api_key("knowledge/gemini") == "k"
    assert calls == [("knowledge/gemini", 300)]


# --- factory -----------------------------------------------------------------------------

ENV = {
    "EMBEDDING_PROVIDER": "gemini",
    "EMBEDDING_MODEL": "gemini-embedding-2",
    "EMBEDDING_DIMENSION": "768",
    "GENERATION_PROVIDER": "CLAUDE",
    "GENERATION_MODEL": "claude-haiku-5-5",
    "GENERATION_THINKING": "disabled",
    "GENERATION_EFFORT": "low",
}


def test_settings_from_env() -> None:
    settings = ProviderSettings.from_env(ENV)
    assert settings.embedding_dimension == 768
    assert settings.embedding_model_id == "gemini/gemini-embedding-2"
    assert embedding_secret_name(settings) == "knowledge/gemini"
    assert generation_secret_name(settings) == "knowledge/anthropic"


def test_factory_builds_the_configured_adapters() -> None:
    settings = ProviderSettings.from_env(ENV)
    embedder = build_embedder(settings, policy=INDEXING_RETRIES, key_loader=lambda _s: "k")
    generator = build_generator(settings, policy=QUERY_RETRIES, key_loader=lambda _s: "k")
    assert isinstance(embedder, GeminiEmbedder)
    assert isinstance(generator, ClaudeGenerator)

    voyage = ProviderSettings.from_env(
        {**ENV, "EMBEDDING_PROVIDER": "voyage", "EMBEDDING_DIMENSION": "1024"}
    )
    assert isinstance(
        build_embedder(voyage, policy=INDEXING_RETRIES, key_loader=lambda _s: "k"), VoyageEmbedder
    )
    gemini = ProviderSettings.from_env(
        {**ENV, "GENERATION_PROVIDER": "GEMINI", "GENERATION_THINKING": "minimal"}
    )
    assert isinstance(
        build_generator(gemini, policy=QUERY_RETRIES, key_loader=lambda _s: "k"), GeminiGenerator
    )
    assert generation_secret_name(gemini) == "knowledge/gemini"


def test_factory_rejects_unknown_providers() -> None:
    with pytest.raises(ValueError, match="embedding provider"):
        build_embedder(
            ProviderSettings.from_env({**ENV, "EMBEDDING_PROVIDER": "openai"}),
            policy=INDEXING_RETRIES,
        )
    with pytest.raises(ValueError, match="generation provider"):
        build_generator(
            ProviderSettings.from_env({**ENV, "GENERATION_PROVIDER": "X"}), policy=QUERY_RETRIES
        )


def test_api_key_is_loaded_per_request_from_the_provider_secret() -> None:
    """Keys come from Secrets Manager (cached by Powertools), so rotation is picked up."""
    loaded: list[str] = []

    def loader(secret: str) -> str:
        loaded.append(secret)
        return "rotating-key"

    stub = Stub([ok({"embeddings": [{"values": [0.0] * 768}]})])
    settings = ProviderSettings.from_env(ENV)
    embedder = build_embedder(
        settings, policy=INDEXING_RETRIES, key_loader=loader, client=stub.caller()._client
    )
    assert loaded == []  # nothing fetched until a call is made
    embedder.embed_query("q")
    embedder.embed_query("q")
    assert loaded == ["knowledge/gemini", "knowledge/gemini"]
    assert stub.requests[0].headers["x-goog-api-key"] == "rotating-key"

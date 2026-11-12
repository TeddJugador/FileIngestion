"""Opt-in: one real call per configured provider (``pytest -m integration``).

Confirms the request formats built from the providers' documentation work against the
real APIs, using the keys stored with scripts/set_provider_key.sh. Costs a fraction of
a cent. Skips a provider whose secret isn't set up or readable.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from knowledge.providers import EmbeddingDocument, GenerationRequest
from knowledge.providers.factory import (
    ProviderSettings,
    build_embedder,
    build_generator,
    embedding_secret_name,
    generation_secret_name,
)
from knowledge.providers.http import QUERY_RETRIES
from knowledge.providers.secrets import MissingApiKeyError, load_api_key

pytestmark = pytest.mark.integration

CONTEXT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "cdk.json").read_text()
)["context"]


def _settings(**overrides: str) -> ProviderSettings:
    env = {
        "EMBEDDING_PROVIDER": CONTEXT["embeddingProvider"],
        "EMBEDDING_MODEL": CONTEXT["embeddingModel"],
        "EMBEDDING_DIMENSION": str(CONTEXT["embeddingDimension"]),
        "GENERATION_PROVIDER": CONTEXT["generationProvider"],
        "GENERATION_MODEL": CONTEXT["generationModel"],
        "GENERATION_THINKING": CONTEXT.get("generationThinking", ""),
        "GENERATION_EFFORT": CONTEXT.get("generationEffort", ""),
    }
    return ProviderSettings.from_env({**env, **overrides})


def _require_secret(name: str) -> None:
    try:
        load_api_key(name)
    except (ClientError, BotoCoreError, MissingApiKeyError) as exc:
        pytest.skip(f"secret {name} not available: {type(exc).__name__}")


def test_configured_embedder_returns_normalised_vectors_of_the_right_size() -> None:
    settings = _settings()
    _require_secret(embedding_secret_name(settings))
    embedder = build_embedder(settings, policy=QUERY_RETRIES)
    docs = embedder.embed_documents(
        [
            EmbeddingDocument(
                "Fee Policy > Due dates", "School fees are due on the 7th of each month."
            ),
            EmbeddingDocument(
                "Uniform Policy > Shoes", "Learners wear black leather school shoes."
            ),
        ]
    )
    query = embedder.embed_query("When do I have to pay school fees?")
    assert len(query) == settings.embedding_dimension
    assert all(len(v) == settings.embedding_dimension for v in docs)

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    # The fee chunk must be the better match: the retrieval hints are doing their job.
    assert cosine(query, docs[0]) > cosine(query, docs[1])


def test_configured_generator_answers_within_budget_and_cites() -> None:
    settings = _settings()
    _require_secret(generation_secret_name(settings))
    generator = build_generator(settings, policy=QUERY_RETRIES)
    result = generator.generate(
        GenerationRequest(
            system="Answer only from the sources. Cite them like [S1].",
            user='<source id="S1">School fees are due on the 7th of each month.</source>\n'
            "Question: When are fees due?",
            max_tokens=600,
        )
    )
    assert result.text.strip()
    assert "[S1]" in result.text
    assert result.truncated is False  # the thinking setting left room for the answer
    assert result.output_tokens > 0

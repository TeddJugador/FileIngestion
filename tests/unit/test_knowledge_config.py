from __future__ import annotations

from typing import Any

import aws_cdk as cdk
import pytest

from infra.knowledge_config import KnowledgeConfig, KnowledgeConfigError
from tests.infra.conftest import cdk_context


def _config(**overrides: Any) -> KnowledgeConfig:
    return KnowledgeConfig.from_node(cdk.App(context=cdk_context(**overrides)).node)


def test_defaults_from_cdk_json() -> None:
    config = _config()
    assert config.index_name == "docs-gemini-768-v1"
    assert config.embedding_model_id == "gemini/gemini-embedding-2"
    assert config.stage == "dev"
    assert config.generation_model == "claude-haiku-5-5"
    assert (config.generation_thinking, config.generation_effort) == ("disabled", "low")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"embeddingProvider": "openai"}, "embeddingProvider"),
        ({"generationProvider": "claude"}, "generationProvider"),  # values are uppercase
        ({"embeddingDimension": 0}, "1-4096"),
        ({"embeddingDimension": 4097}, "1-4096"),
        ({"vectorIndexVersion": 0}, "vectorIndexVersion"),
        ({"stage": "test"}, "stage"),
        ({"corsAllowedOrigins": ["*"]}, "exact origins"),
        ({"corsAllowedOrigins": ["https://app.example.org/path"]}, "exact origins"),
        ({"embeddingModel": ""}, "embeddingModel"),
        ({"embeddingProvider": "voyage", "embeddingDimension": 768}, "Voyage embeddingDimension"),
        ({"embeddingDimension": 64}, "Gemini embeddingDimension"),
        ({"generationEffort": "extreme"}, "generationEffort"),
    ],
)
def test_invalid_config_is_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(KnowledgeConfigError, match=message):
        _config(**overrides)

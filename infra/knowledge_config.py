"""Knowledge-service configuration from ``cdk.json`` context (validated in one place).

Model names are configuration, never code. The vector index is bound to the embedding
provider and dimension that produce its vectors, so its name is derived from them:
``docs-<provider>-<dimension>-v<n>``. Changing the embedding model means a new index
(bump ``vectorIndexVersion``) and a re-index, never mixing vectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from constructs import Node

EMBEDDING_PROVIDERS = ("gemini", "voyage")
# Output sizes each provider can return (checked against their docs on 2026-11-11).
GEMINI_DIMENSIONS = range(128, 3073)
VOYAGE_DIMENSIONS = (256, 512, 1024, 2048)
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
GENERATION_PROVIDERS = ("CLAUDE", "GEMINI")
MAX_S3_VECTORS_DIMENSION = 4096
STAGES = ("dev", "prod")


class KnowledgeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeConfig:
    stage: str
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    vector_index_version: int
    generation_provider: str
    generation_model: str
    # Provider-specific: Claude's thinking type ("disabled", "adaptive", ...) or Gemini's
    # thinkingLevel ("minimal", "low", ...). Thinking counts toward the answer budget.
    generation_thinking: str
    generation_effort: str  # Claude output_config.effort; ignored for Gemini
    cors_allowed_origins: tuple[str, ...]

    @property
    def index_name(self) -> str:
        provider, dimension = self.embedding_provider, self.embedding_dimension
        return f"docs-{provider}-{dimension}-v{self.vector_index_version}"

    @property
    def embedding_model_id(self) -> str:
        """Stored per indexed version, e.g. ``gemini/gemini-embedding-2``."""
        return f"{self.embedding_provider}/{self.embedding_model}"

    @classmethod
    def from_node(cls, node: Node) -> KnowledgeConfig:
        def get(key: str) -> Any:  # noqa: ANN401 - raw cdk.json context
            value = node.try_get_context(key)
            if value is None or value == "":
                raise KnowledgeConfigError(f"missing cdk.json context value '{key}'")
            return value

        config = cls(
            stage=str(node.try_get_context("stage") or "dev"),
            embedding_provider=str(get("embeddingProvider")),
            embedding_model=str(get("embeddingModel")),
            embedding_dimension=int(get("embeddingDimension")),
            vector_index_version=int(get("vectorIndexVersion")),
            generation_provider=str(get("generationProvider")),
            generation_model=str(get("generationModel")),
            generation_thinking=str(node.try_get_context("generationThinking") or ""),
            generation_effort=str(node.try_get_context("generationEffort") or ""),
            cors_allowed_origins=tuple(get("corsAllowedOrigins")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.stage not in STAGES:
            raise KnowledgeConfigError(f"stage must be one of {STAGES}")
        if self.embedding_provider not in EMBEDDING_PROVIDERS:
            raise KnowledgeConfigError(f"embeddingProvider must be one of {EMBEDDING_PROVIDERS}")
        if self.generation_provider not in GENERATION_PROVIDERS:
            raise KnowledgeConfigError(f"generationProvider must be one of {GENERATION_PROVIDERS}")
        if not 1 <= self.embedding_dimension <= MAX_S3_VECTORS_DIMENSION:
            raise KnowledgeConfigError("embeddingDimension must be 1-4096 (S3 Vectors limit)")
        if (
            self.embedding_provider == "gemini"
            and self.embedding_dimension not in GEMINI_DIMENSIONS
        ):
            raise KnowledgeConfigError("Gemini embeddingDimension must be 128-3072")
        if (
            self.embedding_provider == "voyage"
            and self.embedding_dimension not in VOYAGE_DIMENSIONS
        ):
            raise KnowledgeConfigError(
                f"Voyage embeddingDimension must be one of {VOYAGE_DIMENSIONS}"
            )
        if self.generation_provider == "CLAUDE" and self.generation_effort not in (
            "",
            *CLAUDE_EFFORTS,
        ):
            raise KnowledgeConfigError(f"generationEffort must be one of {CLAUDE_EFFORTS}")
        if self.vector_index_version < 1:
            raise KnowledgeConfigError("vectorIndexVersion must be >= 1")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.index_name):
            raise KnowledgeConfigError(f"invalid vector index name {self.index_name!r}")
        for origin in self.cors_allowed_origins:
            if origin == "*" or not re.fullmatch(r"https?://[^/\s]+", origin):
                raise KnowledgeConfigError(
                    f"corsAllowedOrigins entries must be exact origins, not {origin!r}"
                )

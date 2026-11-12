"""Embedding and generation providers behind two small interfaces (spec section 3)."""

from knowledge.providers.base import (
    Embedder,
    EmbeddingDocument,
    GenerationRequest,
    GenerationResult,
    Generator,
)
from knowledge.providers.errors import (
    EmbeddingDimensionError,
    EmbeddingModelMismatchError,
    ProviderError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderRetryableError,
    check_embedding_model,
)

__all__ = [
    "Embedder",
    "EmbeddingDimensionError",
    "EmbeddingDocument",
    "EmbeddingModelMismatchError",
    "GenerationRequest",
    "GenerationResult",
    "Generator",
    "ProviderError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderRetryableError",
    "check_embedding_model",
]

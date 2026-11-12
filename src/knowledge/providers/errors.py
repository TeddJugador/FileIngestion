"""Provider errors. Messages never include request or response bodies: those can echo
a user's question or a document's text, and exceptions end up in logs."""

from __future__ import annotations


class ProviderError(Exception):
    def __init__(
        self, provider: str, message: str, *, status_code: int | None = None, code: str = ""
    ) -> None:
        detail = f" (HTTP {status_code}{', ' + code if code else ''})" if status_code else ""
        super().__init__(f"{provider}: {message}{detail}")
        self.provider = provider
        self.status_code = status_code
        self.code = code

    retryable = False


class ProviderRetryableError(ProviderError):
    """429, 5xx, timeouts or connection errors that outlasted the retry budget.

    The indexer re-raises these so SQS retries the message later."""

    retryable = True


class ProviderRequestError(ProviderError):
    """A non-retryable 4xx: bad key, bad model name, invalid input. Retrying won't help."""


class ProviderResponseError(ProviderError):
    """The provider answered 2xx but the body wasn't what the contract promises."""


class EmbeddingDimensionError(ProviderResponseError):
    """A returned vector's length differs from the configured embeddingDimension."""


class EmbeddingModelMismatchError(Exception):
    """The configured embedder isn't the model that built the index being queried."""

    def __init__(self, configured: str, indexed: str) -> None:
        super().__init__(f"configured embedding model {configured!r} != index model {indexed!r}")
        self.configured = configured
        self.indexed = indexed


def check_embedding_model(configured_model_id: str, index_model_id: str) -> None:
    """Refuse to query an index with vectors from a different model (spec section 3)."""
    if configured_model_id != index_model_id:
        raise EmbeddingModelMismatchError(configured_model_id, index_model_id)

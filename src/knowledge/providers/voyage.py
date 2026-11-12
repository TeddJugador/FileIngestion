"""Voyage AI embeddings (REST, no SDK). Checked against Voyage's docs on 2026-11-11.

- ``input_type`` ``document`` / ``query`` (embedding both the same way hurts retrieval).
- voyage-4 family: ``output_dimension`` 256, 512, 1024 (default) or 2048; 32K tokens
  per input; up to 1,000 inputs per request and, depending on the model, 120K-1M tokens.
- ``truncation: false`` so an over-long input fails instead of being cut silently.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from knowledge.providers.base import EmbeddingDocument
from knowledge.providers.batching import batches
from knowledge.providers.errors import ProviderResponseError
from knowledge.providers.gemini import check_vectors
from knowledge.providers.http import JsonCaller

VOYAGE_API = "https://api.voyageai.com/v1/embeddings"
PROVIDER = "voyage"
SUPPORTED_DIMENSIONS = frozenset({256, 512, 1024, 2048})
MAX_BATCH_SIZE = 128  # well under the 1,000-input limit; keeps requests small
MAX_BATCH_TOKENS = 120_000  # the lowest per-request limit across the voyage-4 family
INPUT_TOKENS = 32_000


class VoyageEmbedder:
    max_batch_size = MAX_BATCH_SIZE
    max_input_tokens = INPUT_TOKENS

    def __init__(
        self, *, model: str, dimension: int, api_key: Callable[[], str], caller: JsonCaller
    ) -> None:
        if dimension not in SUPPORTED_DIMENSIONS:
            raise ValueError(f"Voyage dimensions are {sorted(SUPPORTED_DIMENSIONS)}")
        self.model = model
        self.model_id = f"{PROVIDER}/{model}"
        self.dimension = dimension
        self._api_key = api_key
        self._caller = caller

    def embed_documents(self, documents: Sequence[EmbeddingDocument]) -> list[list[float]]:
        return self._embed([d.prefixed_text for d in documents], input_type="document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], input_type="query")[0]

    def _embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batches(
            texts,
            max_items=self.max_batch_size,
            max_tokens=MAX_BATCH_TOKENS,
            max_input_tokens=self.max_input_tokens,
        ):
            response = self._caller.post(
                VOYAGE_API,
                headers={"authorization": f"Bearer {self._api_key()}"},
                body={
                    "input": batch,
                    "model": self.model,
                    "input_type": input_type,
                    "output_dimension": self.dimension,
                    "output_dtype": "float",
                    "truncation": False,
                },
            )
            try:
                items = sorted(response["data"], key=lambda item: int(item["index"]))
                batch_vectors = [list(map(float, item["embedding"])) for item in items]
            except (KeyError, TypeError, ValueError):
                raise ProviderResponseError(PROVIDER, "unexpected embeddings response") from None
            vectors.extend(
                check_vectors(
                    PROVIDER, batch_vectors, expected=len(batch), dimension=self.dimension
                )
            )
        return vectors

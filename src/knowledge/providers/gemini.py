"""Gemini adapters (REST, no SDK). Checked against Google's docs on 2026-11-11.

Embeddings (``gemini-embedding-2``):
- ``task_type`` is not supported. Retrieval hints go into the text instead:
  documents as ``title: <title> | text: <content>``, queries as
  ``task: question answering | query: <content>``.
- Several texts in one ``embedContent`` call produce ONE aggregated vector, so batches
  use ``batchEmbedContents`` (at most 100 requests per call).
- ``outputDimensionality`` lives inside ``embedContentConfig`` (the top-level field is
  deprecated). Reduced dimensions are normalised by the model.

Generation (``generateContent``): ``maxOutputTokens`` includes thinking tokens, so the
thinking level is configuration (``minimal`` for gemini-3.5-flash-lite).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from knowledge.providers.base import EmbeddingDocument, GenerationRequest, GenerationResult
from knowledge.providers.batching import batches
from knowledge.providers.errors import EmbeddingDimensionError, ProviderResponseError
from knowledge.providers.http import JsonCaller

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
PROVIDER = "gemini"

MAX_BATCH_SIZE = 100  # synchronous batchEmbedContents limit
# Conservative per-request token budget (Google documents 20,000 tokens per embedding
# request on Vertex AI). Costs only extra requests.
MAX_BATCH_TOKENS = 20_000
EMBEDDING_INPUT_TOKENS = 8_192  # gemini-embedding-2
QUERY_TASK = "question answering"


def gemini_error_code(body: Any) -> str:  # noqa: ANN401
    return str(body.get("error", {}).get("status", ""))


def document_text(document: EmbeddingDocument) -> str:
    return f"title: {document.context or 'none'} | text: {document.text}"


def query_text(question: str) -> str:
    return f"task: {QUERY_TASK} | query: {question}"


class GeminiEmbedder:
    max_batch_size = MAX_BATCH_SIZE
    max_input_tokens = EMBEDDING_INPUT_TOKENS

    def __init__(
        self, *, model: str, dimension: int, api_key: Callable[[], str], caller: JsonCaller
    ) -> None:
        if not 128 <= dimension <= 3072:
            raise ValueError("Gemini embedding dimensions are 128-3072")
        self.model = model
        self.model_id = f"{PROVIDER}/{model}"
        self.dimension = dimension
        self._api_key = api_key
        self._caller = caller

    def embed_documents(self, documents: Sequence[EmbeddingDocument]) -> list[list[float]]:
        return self._embed([document_text(d) for d in documents])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([query_text(text)])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batches(
            texts,
            max_items=self.max_batch_size,
            max_tokens=MAX_BATCH_TOKENS,
            max_input_tokens=self.max_input_tokens,
        ):
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "embedContentConfig": {
                        "outputDimensionality": self.dimension,
                        "autoTruncate": False,
                    },
                }
                for text in texts
            ]
        }
        response = self._caller.post(
            f"{GEMINI_API}/models/{self.model}:batchEmbedContents",
            headers={"x-goog-api-key": self._api_key()},
            body=body,
        )
        try:
            vectors = [list(map(float, e["values"])) for e in response["embeddings"]]
        except (KeyError, TypeError, ValueError):
            raise ProviderResponseError(PROVIDER, "unexpected embeddings response") from None
        return check_vectors(PROVIDER, vectors, expected=len(texts), dimension=self.dimension)


class GeminiGenerator:
    provider = "GEMINI"

    def __init__(
        self,
        *,
        model: str,
        api_key: Callable[[], str],
        caller: JsonCaller,
        thinking_level: str | None = "minimal",
    ) -> None:
        self.model = model
        self.model_id = f"{PROVIDER}/{model}"
        self._api_key = api_key
        self._caller = caller
        self._thinking_level = thinking_level

    def generate(self, request: GenerationRequest) -> GenerationResult:
        config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if self._thinking_level:
            config["thinkingConfig"] = {"thinkingLevel": self._thinking_level}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        body = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": config,
        }
        response = self._caller.post(
            f"{GEMINI_API}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self._api_key()},
            body=body,
        )
        candidates = response.get("candidates") or []
        if not candidates:
            reason = str(response.get("promptFeedback", {}).get("blockReason", "NO_CANDIDATES"))
            raise ProviderResponseError(PROVIDER, "no answer candidates", code=reason)
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = response.get("usageMetadata", {})
        finish = str(candidate.get("finishReason", ""))
        return GenerationResult(
            text=text,
            provider=self.provider,
            model_id=self.model_id,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            # Billed output, as Claude reports it: Gemini bills thinking tokens as output.
            output_tokens=int(usage.get("candidatesTokenCount", 0))
            + int(usage.get("thoughtsTokenCount", 0)),
            truncated=finish == "MAX_TOKENS",
            stop_reason=finish,
            extra={"thinking_tokens": int(usage.get("thoughtsTokenCount", 0))},
        )


def check_vectors(
    provider: str, vectors: list[list[float]], *, expected: int, dimension: int
) -> list[list[float]]:
    if len(vectors) != expected:
        raise ProviderResponseError(provider, f"expected {expected} vectors, got {len(vectors)}")
    for vector in vectors:
        if len(vector) != dimension:
            raise EmbeddingDimensionError(
                provider, f"vector length {len(vector)} != configured dimension {dimension}"
            )
    return vectors

"""Deterministic stand-ins for tests: no network, no keys, no cost."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from knowledge.providers.base import EmbeddingDocument, GenerationRequest, GenerationResult
from knowledge.tokens import estimate_tokens

_WORD = re.compile(r"[a-z0-9]+")
_SOURCE_ID = re.compile(r'id="(S\d+)"')


@dataclass
class FakeEmbedder:
    """Hash-based bag-of-words vectors: identical text gives identical vectors, and texts
    that share words are closer, so retrieval tests behave sensibly."""

    dimension: int = 64
    model_id: str = "fake/hashed-bag-of-words"
    max_batch_size: int = 16
    max_input_tokens: int = 2_000
    calls: list[int] = field(default_factory=list)  # batch sizes, for assertions

    def embed_documents(self, documents: Sequence[EmbeddingDocument]) -> list[list[float]]:
        self.calls.append(len(documents))
        return [self._vector(d.prefixed_text) for d in documents]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(1)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        if estimate_tokens(text) > self.max_input_tokens:
            raise ValueError("input too long for FakeEmbedder")
        vector = [0.0] * self.dimension
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            vector[0], norm = 1.0, 1.0
        return [v / norm for v in vector]


@dataclass
class FakeGenerator:
    """Echoes the source IDs it was given as citations. ``answer`` overrides the reply
    (e.g. to test that invented citations are dropped)."""

    provider: str = "FAKE"
    model_id: str = "fake/echo"
    answer: str | None = None
    requests: list[GenerationRequest] = field(default_factory=list)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        ids = _SOURCE_ID.findall(request.user)
        text = self.answer if self.answer is not None else self._echo(ids)
        return GenerationResult(
            text=text,
            provider=self.provider,
            model_id=self.model_id,
            input_tokens=estimate_tokens(request.system + request.user),
            output_tokens=estimate_tokens(text),
            stop_reason="end_turn",
        )

    @staticmethod
    def _echo(ids: list[str]) -> str:
        if not ids:
            return "The provided sources don't cover this."
        return " ".join(f"According to the school's documents [{source}]." for source in ids)

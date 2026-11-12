"""Provider interfaces (spec section 3) and the value types they exchange.

Model names are configuration: every adapter receives its model ID from the
environment, which CDK fills from ``cdk.json``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingDocument:
    """A chunk to embed: its context (``<title> > <heading path>``) and its text.

    Adapters combine the two the way their model expects. The spec's generic form is
    ``"<context>\\n\\n<text>"`` (``prefixed_text``); gemini-embedding-2 instead wants
    ``title: <context> | text: <text>``.
    """

    context: str
    text: str

    @property
    def prefixed_text(self) -> str:
        return f"{self.context}\n\n{self.text}" if self.context else self.text


class Embedder(Protocol):
    model_id: str  # "<provider>/<model>", stored with every indexed version
    dimension: int
    max_batch_size: int
    max_input_tokens: int

    def embed_documents(self, documents: Sequence[EmbeddingDocument]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    user: str
    max_tokens: int = 600
    # None means "don't send one": current Claude models reject temperature, and Gemini
    # 3.x recommends the default. Determinism comes from the prompt and thinking level.
    temperature: float | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int  # billed output tokens, including any thinking tokens
    truncated: bool = False  # stopped at max_tokens: the answer may be cut short
    stop_reason: str = ""
    extra: dict[str, int] = field(default_factory=dict)


class Generator(Protocol):
    provider: str  # "CLAUDE" | "GEMINI"
    model_id: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...

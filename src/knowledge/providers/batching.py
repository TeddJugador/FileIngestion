"""Split embedding inputs by both item count and estimated tokens."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from knowledge.tokens import estimate_tokens


class EmbeddingInputTooLongError(ValueError):
    """One input exceeds the model's input limit. The chunker should make this impossible;
    raising beats a provider silently truncating (we also send autoTruncate/truncation off).
    """


def batches(
    texts: Sequence[str], *, max_items: int, max_tokens: int, max_input_tokens: int
) -> Iterator[list[str]]:
    batch: list[str] = []
    batch_tokens = 0
    for text in texts:
        tokens = estimate_tokens(text)
        if tokens > max_input_tokens:
            raise EmbeddingInputTooLongError(
                f"input of ~{tokens} tokens exceeds the model limit of {max_input_tokens}"
            )
        if batch and (len(batch) >= max_items or batch_tokens + tokens > max_tokens):
            yield batch
            batch, batch_tokens = [], 0
        batch.append(text)
        batch_tokens += tokens
    if batch:
        yield batch

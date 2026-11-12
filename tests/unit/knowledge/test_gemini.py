from __future__ import annotations

import pytest

from knowledge.providers.base import EmbeddingDocument, GenerationRequest
from knowledge.providers.batching import EmbeddingInputTooLongError
from knowledge.providers.errors import EmbeddingDimensionError, ProviderResponseError
from knowledge.providers.gemini import GeminiEmbedder, GeminiGenerator
from tests.unit.knowledge.http_stub import Stub, ok

MODEL = "gemini-embedding-2"


def _vectors_for(request_body: dict[str, list[object]], dimension: int = 768) -> dict[str, object]:
    return {"embeddings": [{"values": [0.1] * dimension} for _ in request_body["requests"]]}


def _embedder(stub: Stub, dimension: int = 768) -> GeminiEmbedder:
    return GeminiEmbedder(
        model=MODEL, dimension=dimension, api_key=lambda: "test-key", caller=stub.caller("gemini")
    )


def _echo_stub(dimension: int = 768) -> Stub:
    import json

    return Stub([lambda request: ok(_vectors_for(json.loads(request.content), dimension))])


def test_document_request_shape() -> None:
    stub = _echo_stub()
    vectors = _embedder(stub).embed_documents(
        [
            EmbeddingDocument("Fee Policy > Late payment", "A penalty of 2% applies."),
            EmbeddingDocument("", "No context here."),
        ]
    )
    assert len(vectors) == 2
    assert len(vectors[0]) == 768
    request = stub.requests[0]
    assert str(request.url).endswith(f"/v1beta/models/{MODEL}:batchEmbedContents")
    assert request.headers["x-goog-api-key"] == "test-key"
    first, second = stub.body()["requests"]
    assert first["model"] == f"models/{MODEL}"
    assert first["content"]["parts"][0]["text"] == (
        "title: Fee Policy > Late payment | text: A penalty of 2% applies."
    )
    assert second["content"]["parts"][0]["text"] == "title: none | text: No context here."
    # The current field, not the deprecated top-level outputDimensionality; no truncation.
    assert first["embedContentConfig"] == {"outputDimensionality": 768, "autoTruncate": False}
    assert "outputDimensionality" not in first
    assert "taskType" not in first


def test_query_uses_the_question_answering_task_prefix() -> None:
    stub = _echo_stub()
    _embedder(stub).embed_query("When are fees due?")
    (request,) = stub.body()["requests"]
    assert request["content"]["parts"][0]["text"] == (
        "task: question answering | query: When are fees due?"
    )


def test_batches_at_most_100_per_call() -> None:
    stub = _echo_stub()
    docs = [EmbeddingDocument("T", f"chunk {i}") for i in range(230)]
    assert len(_embedder(stub).embed_documents(docs)) == 230
    assert [len(stub.body(i)["requests"]) for i in range(3)] == [100, 100, 30]


def test_batches_also_respect_a_token_budget() -> None:
    stub = _echo_stub()
    docs = [EmbeddingDocument("T", "x" * 24_000) for _ in range(5)]  # ~6,000 tokens each
    _embedder(stub).embed_documents(docs)
    assert all(len(stub.body(i)["requests"]) <= 3 for i in range(len(stub.requests)))


def test_wrong_vector_length_is_rejected() -> None:
    stub = _echo_stub(dimension=3072)  # e.g. outputDimensionality ignored
    with pytest.raises(EmbeddingDimensionError):
        _embedder(stub).embed_query("q")


def test_wrong_vector_count_is_rejected() -> None:
    stub = Stub([ok({"embeddings": [{"values": [0.0] * 768}]})])
    with pytest.raises(ProviderResponseError):
        _embedder(stub).embed_documents([EmbeddingDocument("", "a"), EmbeddingDocument("", "b")])


def test_over_long_input_fails_before_any_request() -> None:
    stub = _echo_stub()
    with pytest.raises(EmbeddingInputTooLongError):
        _embedder(stub).embed_documents([EmbeddingDocument("", "x" * 40_000)])
    assert stub.requests == []


@pytest.mark.parametrize("dimension", [64, 4096])
def test_unsupported_dimension(dimension: int) -> None:
    with pytest.raises(ValueError, match="128-3072"):
        _embedder(Stub([ok({})]), dimension=dimension)


# --- generation --------------------------------------------------------------------------


def _generator(stub: Stub, thinking: str | None = "minimal") -> GeminiGenerator:
    return GeminiGenerator(
        model="gemini-3.5-flash-lite",
        api_key=lambda: "test-key",
        caller=stub.caller("gemini"),
        thinking_level=thinking,
    )


def test_generation_request_and_response() -> None:
    stub = Stub(
        [
            ok(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "thinking about fees", "thought": True},
                                    {"text": "Fees are due on the 1st [S1]."},
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 900,
                        "candidatesTokenCount": 12,
                        "thoughtsTokenCount": 30,
                    },
                }
            )
        ]
    )
    result = _generator(stub).generate(GenerationRequest(system="SYS", user="USER", max_tokens=600))
    body = stub.body()
    assert str(stub.requests[0].url).endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert body["generationConfig"] == {
        "maxOutputTokens": 600,
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }
    assert result.text == "Fees are due on the 1st [S1]."  # thought parts excluded
    # output_tokens is what's billed: the answer (12) plus thinking (30).
    assert (result.input_tokens, result.output_tokens) == (900, 42)
    assert result.extra == {"thinking_tokens": 30}
    assert result.truncated is False
    assert result.provider == "GEMINI"


def test_max_tokens_finish_marks_the_answer_truncated() -> None:
    stub = Stub(
        [
            ok(
                {
                    "candidates": [
                        {"content": {"parts": [{"text": "Partial"}]}, "finishReason": "MAX_TOKENS"}
                    ]
                }
            )
        ]
    )
    assert _generator(stub).generate(GenerationRequest(system="s", user="u")).truncated is True


def test_blocked_prompt_is_a_response_error_with_the_reason() -> None:
    stub = Stub([ok({"promptFeedback": {"blockReason": "SAFETY"}})])
    with pytest.raises(ProviderResponseError) as excinfo:
        _generator(stub).generate(GenerationRequest(system="s", user="u"))
    assert excinfo.value.code == "SAFETY"


def test_temperature_and_thinking_are_optional() -> None:
    stub = Stub([ok({"candidates": [{"content": {"parts": [{"text": "x"}]}}]})])
    _generator(stub, thinking=None).generate(
        GenerationRequest(system="s", user="u", temperature=0.2)
    )
    assert stub.body()["generationConfig"] == {"maxOutputTokens": 600, "temperature": 0.2}

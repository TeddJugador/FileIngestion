from __future__ import annotations

import json

import httpx
import pytest

from knowledge.providers.base import EmbeddingDocument, GenerationRequest
from knowledge.providers.claude import ClaudeGenerator, claude_error_code
from knowledge.providers.errors import ProviderRequestError
from knowledge.providers.voyage import VoyageEmbedder
from tests.unit.knowledge.http_stub import Stub, ok, status

# --- Voyage ------------------------------------------------------------------------------


def _voyage_reply(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    n = len(body["input"])
    # Deliberately out of order: the adapter must sort by index.
    data = [{"index": i, "embedding": [float(i)] * 1024} for i in reversed(range(n))]
    return ok({"data": data, "usage": {"total_tokens": 10}})


def _voyage(stub: Stub, dimension: int = 1024) -> VoyageEmbedder:
    return VoyageEmbedder(
        model="voyage-4", dimension=dimension, api_key=lambda: "vk", caller=stub.caller("voyage")
    )


def test_voyage_documents_use_document_input_type_and_context_prefix() -> None:
    stub = Stub([_voyage_reply])
    vectors = _voyage(stub).embed_documents(
        [
            EmbeddingDocument("Uniform Policy > Shoes", "Black leather shoes."),
            EmbeddingDocument("", "b"),
        ]
    )
    body = stub.body()
    assert stub.requests[0].headers["authorization"] == "Bearer vk"
    assert body == {
        "input": ["Uniform Policy > Shoes\n\nBlack leather shoes.", "b"],
        "model": "voyage-4",
        "input_type": "document",
        "output_dimension": 1024,
        "output_dtype": "float",
        "truncation": False,
    }
    assert [v[0] for v in vectors] == [0.0, 1.0]  # sorted back into input order


def test_voyage_query_uses_query_input_type() -> None:
    stub = Stub([_voyage_reply])
    _voyage(stub).embed_query("What shoes?")
    assert stub.body()["input_type"] == "query"
    assert stub.body()["input"] == ["What shoes?"]


@pytest.mark.parametrize("dimension", [768, 1536])
def test_voyage_rejects_unsupported_dimensions(dimension: int) -> None:
    with pytest.raises(ValueError, match="256, 512, 1024, 2048"):
        _voyage(Stub([ok({})]), dimension=dimension)


def test_voyage_batches_by_128() -> None:
    stub = Stub([_voyage_reply])
    _voyage(stub).embed_documents([EmbeddingDocument("", f"t{i}") for i in range(300)])
    assert [len(stub.body(i)["input"]) for i in range(3)] == [128, 128, 44]


# --- Claude ------------------------------------------------------------------------------


def _claude(stub: Stub, **kwargs: str | None) -> ClaudeGenerator:
    return ClaudeGenerator(
        model="claude-haiku-5-5",
        api_key=lambda: "ak",
        caller=stub.caller("claude", error_code=claude_error_code),
        **kwargs,  # type: ignore[arg-type]
    )


CLAUDE_REPLY = {
    "content": [
        {"type": "thinking", "thinking": "internal"},
        {"type": "text", "text": "Uniforms are navy [S1]."},
    ],
    "usage": {"input_tokens": 1200, "output_tokens": 15},
    "stop_reason": "end_turn",
}


def test_claude_request_follows_current_model_rules() -> None:
    stub = Stub([ok(CLAUDE_REPLY)])
    result = _claude(stub).generate(GenerationRequest(system="SYS", user="USER", max_tokens=600))
    request = stub.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "ak"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert stub.body() == {
        "model": "claude-haiku-5-5",
        "max_tokens": 600,
        "system": "SYS",
        "messages": [{"role": "user", "content": "USER"}],
        "thinking": {"type": "disabled"},  # all 600 tokens go to the answer
        "output_config": {"effort": "low"},
    }
    assert "temperature" not in stub.body()  # current Claude models reject it
    assert result.text == "Uniforms are navy [S1]."  # thinking blocks ignored
    assert (result.input_tokens, result.output_tokens) == (1200, 15)
    assert result.provider == "CLAUDE"
    assert result.model_id == "claude/claude-haiku-5-5"


def test_claude_truncation_is_reported() -> None:
    stub = Stub([ok({**CLAUDE_REPLY, "stop_reason": "max_tokens"})])
    assert _claude(stub).generate(GenerationRequest(system="s", user="u")).truncated is True


def test_claude_thinking_and_effort_come_from_configuration() -> None:
    stub = Stub([ok(CLAUDE_REPLY)])
    _claude(stub, thinking=None, effort=None).generate(GenerationRequest(system="s", user="u"))
    assert "thinking" not in stub.body()
    assert "output_config" not in stub.body()


def test_claude_bad_request_reports_the_error_type_only() -> None:
    stub = Stub(
        [
            status(
                400,
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "secret text"},
                },
            )
        ]
    )
    with pytest.raises(ProviderRequestError) as excinfo:
        _claude(stub).generate(GenerationRequest(system="s", user="u"))
    assert excinfo.value.code == "invalid_request_error"
    assert "secret text" not in str(excinfo.value)


def test_claude_overloaded_is_retried() -> None:
    stub = Stub([status(529), ok(CLAUDE_REPLY)])
    assert _claude(stub).generate(GenerationRequest(system="s", user="u")).text
    assert len(stub.requests) == 2

"""Claude via the Messages API (REST, no SDK). Checked against Anthropic's docs on
2026-11-11.

Current models (Claude 5.5 family) differ from older ones in ways that matter here:
- ``temperature``/``top_p``/``top_k`` must be omitted; ``output_config.effort`` steers
  how much work the model does (``low`` is recommended for chat-style requests).
- Adaptive thinking is on by default and counts toward ``max_tokens``. With a 600-token
  answer budget, thinking is set from configuration: ``disabled`` on Claude Haiku 5.5
  (Sonnet 5.5 uses ``between_tools``; Opus 5.5 can't disable it).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from knowledge.providers.base import GenerationRequest, GenerationResult
from knowledge.providers.errors import ProviderResponseError
from knowledge.providers.http import JsonCaller

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
PROVIDER = "claude"


def claude_error_code(body: Any) -> str:  # noqa: ANN401
    return str(body.get("error", {}).get("type", ""))


class ClaudeGenerator:
    provider = "CLAUDE"

    def __init__(
        self,
        *,
        model: str,
        api_key: Callable[[], str],
        caller: JsonCaller,
        thinking: str | None = "disabled",
        effort: str | None = "low",
    ) -> None:
        self.model = model
        self.model_id = f"{PROVIDER}/{model}"
        self._api_key = api_key
        self._caller = caller
        self._thinking = thinking
        self._effort = effort

    def generate(self, request: GenerationRequest) -> GenerationResult:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        if self._thinking:
            body["thinking"] = {"type": self._thinking}
        if self._effort:
            body["output_config"] = {"effort": self._effort}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        response = self._caller.post(
            ANTHROPIC_API,
            headers={"x-api-key": self._api_key(), "anthropic-version": ANTHROPIC_VERSION},
            body=body,
        )
        try:
            blocks = response["content"]
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            usage = response.get("usage", {})
            stop_reason = str(response.get("stop_reason", ""))
        except (KeyError, TypeError, AttributeError):
            raise ProviderResponseError(PROVIDER, "unexpected messages response") from None
        return GenerationResult(
            text=text,
            provider=self.provider,
            model_id=self.model_id,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            truncated=stop_reason == "max_tokens",
            stop_reason=stop_reason,
        )

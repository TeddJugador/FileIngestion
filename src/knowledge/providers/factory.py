"""Build the configured embedder and generator from environment variables (which CDK
fills from cdk.json). Swapping providers is a configuration change, not a code change."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from knowledge.providers.base import Embedder, Generator
from knowledge.providers.claude import ClaudeGenerator, claude_error_code
from knowledge.providers.gemini import GeminiEmbedder, GeminiGenerator, gemini_error_code
from knowledge.providers.http import ErrorObserver, JsonCaller, RetryPolicy
from knowledge.providers.secrets import SECRET_NAMES, load_api_key
from knowledge.providers.voyage import VoyageEmbedder

KeyLoader = Callable[[str], str]


@dataclass(frozen=True)
class ProviderSettings:
    embedding_provider: str  # gemini | voyage
    embedding_model: str
    embedding_dimension: int
    generation_provider: str  # CLAUDE | GEMINI
    generation_model: str
    generation_thinking: str  # provider-specific: Claude thinking type / Gemini level
    generation_effort: str  # Claude output_config.effort; ignored by Gemini

    @property
    def embedding_model_id(self) -> str:
        return f"{self.embedding_provider}/{self.embedding_model}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> ProviderSettings:
        return cls(
            embedding_provider=env["EMBEDDING_PROVIDER"],
            embedding_model=env["EMBEDDING_MODEL"],
            embedding_dimension=int(env["EMBEDDING_DIMENSION"]),
            generation_provider=env.get("GENERATION_PROVIDER", ""),
            generation_model=env.get("GENERATION_MODEL", ""),
            generation_thinking=env.get("GENERATION_THINKING", ""),
            generation_effort=env.get("GENERATION_EFFORT", ""),
        )


def embedding_secret_name(settings: ProviderSettings) -> str:
    if settings.embedding_provider not in ("gemini", "voyage"):
        raise ValueError(f"unknown embedding provider {settings.embedding_provider!r}")
    return SECRET_NAMES[settings.embedding_provider]


def generation_secret_name(settings: ProviderSettings) -> str:
    names = {"CLAUDE": SECRET_NAMES["claude"], "GEMINI": SECRET_NAMES["gemini"]}
    if settings.generation_provider not in names:
        raise ValueError(f"unknown generation provider {settings.generation_provider!r}")
    return names[settings.generation_provider]


def build_embedder(
    settings: ProviderSettings,
    *,
    policy: RetryPolicy,
    key_loader: KeyLoader = load_api_key,
    observer: ErrorObserver | None = None,
    client: httpx.Client | None = None,
) -> Embedder:
    secret = embedding_secret_name(settings)

    def api_key() -> str:
        return key_loader(secret)

    if settings.embedding_provider == "gemini":
        caller = JsonCaller(
            "gemini", policy, client=client, observer=observer, error_code=gemini_error_code
        )
        return GeminiEmbedder(
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            api_key=api_key,
            caller=caller,
        )
    if settings.embedding_provider == "voyage":
        caller = JsonCaller("voyage", policy, client=client, observer=observer)
        return VoyageEmbedder(
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            api_key=api_key,
            caller=caller,
        )
    raise ValueError(f"unknown embedding provider {settings.embedding_provider!r}")


def build_generator(
    settings: ProviderSettings,
    *,
    policy: RetryPolicy,
    key_loader: KeyLoader = load_api_key,
    observer: ErrorObserver | None = None,
    client: httpx.Client | None = None,
) -> Generator:
    secret = generation_secret_name(settings)

    def api_key() -> str:
        return key_loader(secret)

    thinking = settings.generation_thinking or None
    if settings.generation_provider == "CLAUDE":
        caller = JsonCaller(
            "claude", policy, client=client, observer=observer, error_code=claude_error_code
        )
        return ClaudeGenerator(
            model=settings.generation_model,
            api_key=api_key,
            caller=caller,
            thinking=thinking,
            effort=settings.generation_effort or None,
        )
    if settings.generation_provider == "GEMINI":
        caller = JsonCaller(
            "gemini", policy, client=client, observer=observer, error_code=gemini_error_code
        )
        return GeminiGenerator(
            model=settings.generation_model,
            api_key=api_key,
            caller=caller,
            thinking_level=thinking,
        )
    raise ValueError(f"unknown generation provider {settings.generation_provider!r}")

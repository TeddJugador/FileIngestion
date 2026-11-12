"""Provider API keys from Secrets Manager (one secret per provider), cached 5 minutes.

Keys are never in environment variables, code, logs or Git. A secret may hold the key
as plain text or as JSON ``{"apiKey": "..."}``.
"""

from __future__ import annotations

import json

from aws_lambda_powertools.utilities import parameters

CACHE_SECONDS = 300
SECRET_NAMES = {
    "gemini": "knowledge/gemini",
    "voyage": "knowledge/voyage",
    "claude": "knowledge/anthropic",
}


class MissingApiKeyError(RuntimeError):
    pass


def parse_secret(value: str | bytes) -> str:
    text = value.decode() if isinstance(value, bytes) else value
    text = text.strip()
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("apiKey", "")).strip()
        except (ValueError, AttributeError):
            text = ""
    if not text:
        raise MissingApiKeyError("secret is empty or has no apiKey field")
    return text


def load_api_key(secret_name: str) -> str:
    """Fetched at cold start, then served from Powertools' cache for 5 minutes."""
    return parse_secret(parameters.get_secret(secret_name, max_age=CACHE_SECONDS))

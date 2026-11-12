"""Per-vector metadata for S3 Vectors, with its size limits enforced before writing.

Limits (AWS docs, checked 2026-11-11): 2 KB of filterable metadata and 40 KB in total
per vector, at most 10 non-filterable keys per index. The non-filterable keys are
declared on the index (infra/constructs/knowledge/limits.py); a test keeps the two
lists identical. Oversized metadata raises instead of being truncated.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from knowledge.chunking import Chunk

FILTERABLE_KEYS = ("school_id", "document_id", "version", "audience")
NON_FILTERABLE_KEYS = ("chunk_text", "title", "page_start", "page_end", "heading_path")
MAX_FILTERABLE_BYTES = 2 * 1024
MAX_TOTAL_BYTES = 40 * 1024


class MetadataTooLargeError(ValueError):
    pass


def _size(metadata: dict[str, Any], keys: Sequence[str]) -> int:
    subset = {k: metadata[k] for k in keys if k in metadata}
    return len(json.dumps(subset, ensure_ascii=False, separators=(",", ":")).encode())


def check_metadata_size(metadata: dict[str, Any]) -> None:
    if _size(metadata, FILTERABLE_KEYS) > MAX_FILTERABLE_BYTES:
        raise MetadataTooLargeError("filterable metadata exceeds 2 KB")
    if _size(metadata, list(metadata)) > MAX_TOTAL_BYTES:
        raise MetadataTooLargeError("vector metadata exceeds 40 KB")


def vector_key(document_id: str, version: str, chunk_no: int) -> str:
    return f"{document_id}#{version}#{chunk_no:05d}"


def build_metadata(
    chunk: Chunk,
    *,
    school_id: str,
    document_id: str,
    version: str,
    audience: Sequence[str],
    title: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "school_id": school_id,
        "document_id": document_id,
        "version": version,
        "audience": list(audience),
        "chunk_text": chunk.text,
        "title": title,
        "heading_path": " > ".join(chunk.heading_path),
    }
    if chunk.page_start is not None and chunk.page_end is not None:
        metadata["page_start"] = chunk.page_start
        metadata["page_end"] = chunk.page_end
    check_metadata_size(metadata)
    return metadata

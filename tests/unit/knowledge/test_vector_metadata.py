from __future__ import annotations

import pytest

from infra.constructs.knowledge.limits import FILTERABLE_METADATA_KEYS, NON_FILTERABLE_METADATA_KEYS
from knowledge.chunking import MAX_TOKENS, Chunk
from knowledge.vector_metadata import (
    FILTERABLE_KEYS,
    NON_FILTERABLE_KEYS,
    MetadataTooLargeError,
    build_metadata,
    check_metadata_size,
    vector_key,
)


def _chunk(
    text: str = "Fees are due on the 7th.",
    heading: tuple[str, ...] = ("Payment",),
    pages: tuple[int, int] | None = (2, 3),
) -> Chunk:
    return Chunk(
        chunk_no=0,
        text=text,
        context="Fee Policy > Payment",
        heading_path=heading,
        page_start=pages[0] if pages else None,
        page_end=pages[1] if pages else None,
    )


def _metadata(chunk: Chunk, title: str = "Fee Policy 2027") -> dict[str, object]:
    return build_metadata(
        chunk,
        school_id="SCH-001",
        document_id="doc-4be1a07c93d2",
        version="v-9f2c1e0ab3d4",
        audience=["STAFF", "PARENT"],
        title=title,
    )


def test_metadata_keys_match_the_index_definition() -> None:
    """The keys the code writes must be the keys the CDK index declares."""
    assert FILTERABLE_KEYS == FILTERABLE_METADATA_KEYS
    assert NON_FILTERABLE_KEYS == NON_FILTERABLE_METADATA_KEYS
    assert len(NON_FILTERABLE_KEYS) <= 10  # S3 Vectors limit per index


def test_metadata_contents() -> None:
    metadata = _metadata(_chunk())
    assert metadata == {
        "school_id": "SCH-001",
        "document_id": "doc-4be1a07c93d2",
        "version": "v-9f2c1e0ab3d4",
        "audience": ["STAFF", "PARENT"],
        "chunk_text": "Fees are due on the 7th.",
        "title": "Fee Policy 2027",
        "heading_path": "Payment",
        "page_start": 2,
        "page_end": 3,
    }
    assert set(metadata) <= set(FILTERABLE_KEYS) | set(NON_FILTERABLE_KEYS)


def test_pages_are_omitted_rather_than_null() -> None:
    metadata = _metadata(_chunk(pages=None))
    assert "page_start" not in metadata
    assert "page_end" not in metadata


def test_worst_case_chunk_fits() -> None:
    # The largest chunk, in 4-byte characters, with a 120-character title.
    _metadata(_chunk(text="😀" * (MAX_TOKENS * 4)), title="T" * 120)


def test_oversized_metadata_fails_fast_instead_of_truncating() -> None:
    with pytest.raises(MetadataTooLargeError, match="40 KB"):
        _metadata(_chunk(heading=("H" * 50_000,)))


def test_filterable_limit_is_enforced_separately() -> None:
    with pytest.raises(MetadataTooLargeError, match="2 KB"):
        check_metadata_size({"school_id": "S" * 3000, "chunk_text": "x"})


def test_vector_key_format() -> None:
    assert (
        vector_key("doc-4be1a07c93d2", "v-9f2c1e0ab3d4", 7)
        == "doc-4be1a07c93d2#v-9f2c1e0ab3d4#00007"
    )

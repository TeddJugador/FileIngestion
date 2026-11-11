from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from ingestion.schemas.common import ValidationContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AS_OF = date(2026, 11, 11)


def chunked(data: bytes, size: int = 7) -> Iterator[bytes]:
    """Small, awkward chunk sizes so multi-byte characters and CRLFs straddle chunks."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


@pytest.fixture
def context() -> ValidationContext:
    return ValidationContext(as_of=AS_OF)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


from tests.unit.aws import harness  # noqa: E402, F401  (re-exported fixture)

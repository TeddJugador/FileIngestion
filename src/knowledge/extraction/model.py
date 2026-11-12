"""What extraction produces, and how it fails."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExtractionFailure(StrEnum):
    """``DocumentIndexingFailed.reasonCode`` values that extraction can produce."""

    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"  # usually a scanned PDF (no OCR in v1)
    EXTRACTION_FAILED = "EXTRACTION_FAILED"  # corrupt, encrypted or unreadable


class ExtractionError(Exception):
    """A permanent failure: retrying the same bytes gives the same result.

    ``detail`` is content-free (it may be logged): describe the problem, never quote text.
    """

    def __init__(self, reason: ExtractionFailure, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    TABLE = "table"  # rows joined by newlines, cells by " | "
    LIST = "list"  # items joined by newlines
    CODE = "code"


@dataclass(frozen=True)
class Block:
    """One structural unit of a document, in reading order."""

    text: str
    heading_path: tuple[str, ...] = ()
    page: int | None = None  # 1-based, PDFs only
    kind: BlockKind = BlockKind.PARAGRAPH


@dataclass(frozen=True)
class ExtractedDocument:
    blocks: tuple[Block, ...]
    page_count: int | None = None  # PDFs only

    @property
    def text_length(self) -> int:
        return sum(len(b.text) for b in self.blocks)

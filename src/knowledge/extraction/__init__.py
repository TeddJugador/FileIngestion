"""Text extraction that keeps structure: PDF pages, DOCX headings and tables, Markdown
headings, plain-text paragraphs."""

from __future__ import annotations

from knowledge.extraction.docx_reader import extract_docx
from knowledge.extraction.model import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailure,
)
from knowledge.extraction.pdf import extract_pdf
from knowledge.extraction.text_formats import extract_markdown, extract_text

__all__ = [
    "Block",
    "BlockKind",
    "ExtractedDocument",
    "ExtractionError",
    "ExtractionFailure",
    "extract",
]

_EXTRACTORS = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "md": extract_markdown,
    "txt": extract_text,
}


def extract(data: bytes, extension: str) -> ExtractedDocument:
    """Extract by file extension (ingestion has already checked content matches it).

    Raises ``ExtractionError``; a document with no text at all is NO_EXTRACTABLE_TEXT.
    """
    extractor = _EXTRACTORS.get(extension.lower())
    if extractor is None:
        raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "unsupported extension")
    document = extractor(data)
    if not document.blocks:
        raise ExtractionError(ExtractionFailure.NO_EXTRACTABLE_TEXT, "document has no text")
    return document

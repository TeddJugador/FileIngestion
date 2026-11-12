"""PDF text with page numbers (pypdf). Scanned PDFs have no text layer: v1 has no OCR."""

from __future__ import annotations

import logging
from io import BytesIO

from pypdf import PdfReader

from knowledge.extraction.model import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailure,
)
from knowledge.extraction.normalise import remove_repeated_lines, split_paragraphs

MIN_CHARS_PER_PAGE = 50  # below this on average: probably scanned

# pypdf logs warnings that can quote document text; keep them out of the Lambda's logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf(data: bytes) -> ExtractedDocument:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "PDF is encrypted")
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "unreadable PDF") from None

    if not page_texts:
        raise ExtractionError(ExtractionFailure.NO_EXTRACTABLE_TEXT, "PDF has no pages")

    pages = remove_repeated_lines([text.splitlines() for text in page_texts])
    text_chars = sum(len("".join(line.split())) for lines in pages for line in lines)
    if text_chars / len(pages) < MIN_CHARS_PER_PAGE:
        raise ExtractionError(
            ExtractionFailure.NO_EXTRACTABLE_TEXT,
            f"{text_chars / len(pages):.0f} characters per page on average (scanned?)",
        )

    blocks = []
    for number, lines in enumerate(pages, start=1):
        for text, is_list in split_paragraphs("\n".join(lines)):
            kind = BlockKind.LIST if is_list else BlockKind.PARAGRAPH
            blocks.append(Block(text=text, page=number, kind=kind))
    return ExtractedDocument(blocks=tuple(blocks), page_count=len(pages))

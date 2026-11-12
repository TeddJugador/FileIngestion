"""DOCX paragraphs with heading levels from paragraph styles, and tables (python-docx)."""

from __future__ import annotations

import re
from io import BytesIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from knowledge.extraction.model import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailure,
)
from knowledge.extraction.normalise import clean_line

_HEADING_STYLE = re.compile(r"^Heading (\d)$")
_LIST_STYLE = re.compile(r"^List")


def heading_level(style_name: str) -> int | None:
    """Heading 1-9 -> 1-9. The "Title" style is 0: the document title comes from the
    upload metadata and already leads every chunk's context, so it isn't repeated in
    heading paths."""
    if style_name == "Title":
        return 0
    match = _HEADING_STYLE.match(style_name)
    return int(match.group(1)) if match else None


def _table_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells, seen = [], set()
        for cell in row.cells:
            if id(cell._tc) in seen:  # a merged cell repeats in row.cells
                continue
            seen.add(id(cell._tc))
            cells.append(clean_line(cell.text.replace("\n", " ")))
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_docx(data: bytes) -> ExtractedDocument:
    try:
        document = Document(BytesIO(data))
        body = document.element.body
        children = list(body.iterchildren())
    except Exception:
        raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "unreadable DOCX") from None

    headings: list[str] = []
    blocks: list[Block] = []
    for child in children:
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = clean_line(paragraph.text.replace("\n", " "))
            if not text:
                continue
            style = paragraph.style.name if paragraph.style is not None else ""
            level = heading_level(style or "")
            if level == 0:
                continue
            if level is not None:
                # Keep ancestors, replace this level, drop deeper ones.
                headings = [*headings[: level - 1], text]
                continue
            kind = BlockKind.LIST if _LIST_STYLE.match(style or "") else BlockKind.PARAGRAPH
            if (
                kind is BlockKind.LIST
                and blocks
                and blocks[-1].kind is BlockKind.LIST
                and (blocks[-1].heading_path == tuple(headings))
            ):
                previous = blocks.pop()  # consecutive list items form one block
                text = f"{previous.text}\n{text}"
            blocks.append(Block(text=text, heading_path=tuple(headings), kind=kind))
        elif child.tag == qn("w:tbl"):
            text = _table_text(Table(child, document))
            if text:
                blocks.append(Block(text=text, heading_path=tuple(headings), kind=BlockKind.TABLE))
    return ExtractedDocument(blocks=tuple(blocks))

"""Markdown (split on ``#`` headings) and plain text (paragraphs)."""

from __future__ import annotations

import re

from knowledge.extraction.model import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailure,
)
from knowledge.extraction.normalise import split_paragraphs

_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "text is not UTF-8") from None


def _paragraph_blocks(text: str, headings: tuple[str, ...]) -> list[Block]:
    return [
        Block(
            text=t, heading_path=headings, kind=BlockKind.LIST if is_list else BlockKind.PARAGRAPH
        )
        for t, is_list in split_paragraphs(text)
    ]


def extract_markdown(data: bytes) -> ExtractedDocument:
    headings: list[str] = []
    blocks: list[Block] = []
    buffer: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        if buffer:
            blocks.extend(_paragraph_blocks("\n".join(buffer), tuple(headings)))
            buffer.clear()

    for line in decode(data).splitlines():
        if _FENCE.match(line):
            if code is None:  # opening fence: a '#' inside code is not a heading
                flush()
                code = []
            else:
                text = "\n".join(code).strip()
                if text:
                    blocks.append(
                        Block(text=text, heading_path=tuple(headings), kind=BlockKind.CODE)
                    )
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        match = _ATX_HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            headings = [*headings[: level - 1], match.group(2).strip()]
            continue
        buffer.append(line)
    if code:  # unclosed fence: keep the text
        buffer.extend(code)
    flush()
    return ExtractedDocument(blocks=tuple(blocks))


def extract_text(data: bytes) -> ExtractedDocument:
    return ExtractedDocument(blocks=tuple(_paragraph_blocks(decode(data), ())))

"""Structure-aware chunking (spec section 5, step 5).

- Chunks never cross a heading boundary, and prefer paragraph boundaries; within a
  paragraph they break between sentences. Only a single sentence longer than the hard
  maximum is split mid-sentence (between words).
- Target 500 tokens, hard maximum 800, ~75 tokens of sentence-aligned overlap between
  consecutive chunks of the same section. Tokens are estimated as len(text) / 4.
- Each chunk's embedded text is prefixed with its context, ``<title> > <heading path>``.
- Chunks under 20 tokens are merged into a neighbour rather than lost (a short
  "Contact" section is still content); one that can't be merged is dropped unless it's
  the document's only chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from knowledge.extraction.model import Block, BlockKind, ExtractedDocument
from knowledge.providers.base import EmbeddingDocument
from knowledge.tokens import CHARS_PER_TOKEN, estimate_tokens

TARGET_TOKENS = 500
MAX_TOKENS = 800
OVERLAP_TOKENS = 75
MIN_TOKENS = 20
KEEP_PARAGRAPH_FILL = 0.6  # start a paragraph in a new chunk if this chunk is this full

_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "no",
        "nos",
        "e.g",
        "i.e",
        "etc",
        "vs",
        "approx",
        "dept",
    }
)
# A sentence ends at . ! or ?, optionally followed by closing quotes or brackets
# (straight or curly), then whitespace, then a capital or digit (possibly quoted).
_CLOSERS = "\"')\\]\u2019\u201d"
_OPENERS = "\"'(\\[\u2018\u201c"
_SENTENCE_BOUNDARY = re.compile(rf"(?<=[.!?])([{_CLOSERS}]*)\s+(?=[{_OPENERS}]?[A-Z0-9])")


class ChunkingError(ValueError):
    """A chunk can't be made to fit the embedder (e.g. an absurdly long title)."""


@dataclass(frozen=True)
class Chunk:
    chunk_no: int
    text: str  # plain text, shown to users and stored as vector metadata
    context: str  # "<title> > <heading path>", prefixed to the embedded text
    heading_path: tuple[str, ...]
    page_start: int | None
    page_end: int | None

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def embedding_document(self) -> EmbeddingDocument:
        return EmbeddingDocument(context=self.context, text=self.text)


@dataclass
class _Unit:
    text: str
    page: int | None
    sep: str  # joins this unit to the previous one: " ", "\n" or "\n\n"
    paragraph_chars: int = 0  # length of the whole paragraph, on its first unit only


@dataclass
class _Draft:
    heading_path: tuple[str, ...]
    units: list[_Unit] = field(default_factory=list)
    overlap: int = 0  # how many leading units were carried over from the previous chunk

    @property
    def chars(self) -> int:
        return _joined_length(self.units)

    @property
    def has_new_content(self) -> bool:
        return len(self.units) > self.overlap


def _joined_length(units: list[_Unit]) -> int:
    return sum(len(u.text) for u in units) + sum(len(u.sep) for u in units[1:])


def _join(units: list[_Unit]) -> str:
    return "".join((u.sep if i else "") + u.text for i, u in enumerate(units))


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences without losing a character: closing quotes and
    brackets stay with the sentence they close. Common abbreviations don't split."""
    sentences, start = [], 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.start() + len(match.group(1))  # keep the closers in this sentence
        before = text[start : match.start()].rsplit(maxsplit=1)
        last_word = before[-1].rstrip(".!?" + _CLOSERS).lower() if before else ""
        if last_word in _ABBREVIATIONS:
            continue
        sentences.append(text[start:end].strip())
        start = match.end()
    sentences.append(text[start:].strip())
    return [s for s in sentences if s]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Last resort for one sentence over the maximum: break between words."""
    pieces, current = [], ""
    for word in text.split(" "):
        while len(word) > max_chars:  # a "word" with no spaces at all (e.g. a long URL)
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _units(block: Block, max_chars: int, first_sep: str) -> list[_Unit]:
    if block.kind in (BlockKind.TABLE, BlockKind.LIST, BlockKind.CODE):
        parts, sep = block.text.split("\n"), "\n"  # rows / items / lines stay whole
    else:
        parts, sep = split_sentences(block.text), " "
    pieces: list[tuple[str, str]] = []
    for part in parts:
        for i, piece in enumerate(
            _hard_split(part, max_chars) if len(part) > max_chars else [part]
        ):
            pieces.append((piece, " " if i else sep))
    units = [
        _Unit(text=text, page=block.page, sep=first_sep if i == 0 else s)
        for i, (text, s) in enumerate(pieces)
    ]
    if units:
        units[0].paragraph_chars = len(block.text)
    return units


def _sections(blocks: tuple[Block, ...]) -> list[tuple[tuple[str, ...], list[Block]]]:
    sections: list[tuple[tuple[str, ...], list[Block]]] = []
    for block in blocks:
        if sections and sections[-1][0] == block.heading_path:
            sections[-1][1].append(block)
        else:
            sections.append((block.heading_path, [block]))
    return sections


def _overlap_tail(units: list[_Unit], max_chars: int) -> list[_Unit]:
    tail: list[_Unit] = []
    for unit in reversed(units):
        if _joined_length([unit, *tail]) > max_chars:
            break
        tail.insert(0, unit)
    if tail:
        tail[0] = replace(tail[0], sep="")
    return tail


def chunk_document(
    document: ExtractedDocument,
    *,
    title: str,
    max_input_tokens: int = 8_192,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    min_tokens: int = MIN_TOKENS,
) -> list[Chunk]:
    target_chars = target_tokens * CHARS_PER_TOKEN
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    drafts: list[_Draft] = []
    for heading_path, blocks in _sections(document.blocks):
        draft = _Draft(heading_path)
        for b, block in enumerate(blocks):
            for unit in _units(block, max_chars, first_sep="\n\n" if b else ""):
                if not draft.units:
                    unit.sep = ""
                    draft.units.append(unit)
                    continue
                grown = draft.chars + len(unit.sep) + len(unit.text)
                starts_paragraph = unit.paragraph_chars > 0
                keep_paragraph_whole = (
                    starts_paragraph
                    and draft.chars + len(unit.sep) + unit.paragraph_chars > target_chars
                    and draft.chars >= KEEP_PARAGRAPH_FILL * target_chars
                )
                if grown <= target_chars and not keep_paragraph_whole:
                    draft.units.append(unit)
                    continue
                if draft.has_new_content:
                    drafts.append(draft)
                    tail = _overlap_tail(draft.units, overlap_chars)
                    draft = _Draft(heading_path, units=tail, overlap=len(tail))
                if draft.units and draft.chars + len(unit.sep) + len(unit.text) > max_chars:
                    draft = _Draft(heading_path)  # overlap would push this unit past the max
                unit.sep = unit.sep if draft.units else ""
                draft.units.append(unit)
        if draft.has_new_content:
            drafts.append(draft)

    drafts = _merge_small(drafts, min_chars=min_tokens * CHARS_PER_TOKEN, max_chars=max_chars)
    chunks = [_to_chunk(i, d, title) for i, d in enumerate(drafts)]
    for chunk in chunks:
        if estimate_tokens(chunk.embedding_document.prefixed_text) > max_input_tokens:
            raise ChunkingError("chunk plus context exceeds the embedder's input limit")
    return chunks


def _merge_small(drafts: list[_Draft], *, min_chars: int, max_chars: int) -> list[_Draft]:
    """Fold chunks under the minimum into a neighbour, labelled with their heading."""
    if len(drafts) <= 1:
        return drafts
    result: list[_Draft] = []
    pending: list[_Unit] = []  # small content waiting to be prepended to the next draft
    for i, draft in enumerate(drafts):
        if pending:
            merged = [*pending, _relabel(draft.units[0], "\n\n"), *draft.units[1:]]
            if _joined_length(merged) <= max_chars:
                draft = _Draft(draft.heading_path, merged)
            pending = []
        if _joined_length(draft.units) >= min_chars:
            result.append(draft)
            continue
        labelled = _labelled(draft)
        if result and _joined_length([*result[-1].units, *labelled]) <= max_chars:
            labelled[0] = _relabel(labelled[0], "\n\n")
            result[-1].units.extend(labelled)
        elif i + 1 < len(drafts):
            pending = labelled
        # otherwise: too small to stand alone and nowhere to put it -> dropped
    if not result and drafts:
        result = [drafts[0]]  # the document's only content stays, however short
    return result


def _labelled(draft: _Draft) -> list[_Unit]:
    units = [replace(u) for u in draft.units]
    if draft.heading_path:
        heading = _Unit(text=f"{draft.heading_path[-1]}:", page=units[0].page, sep="")
        units[0] = _relabel(units[0], " ")
        return [heading, *units]
    return units


def _relabel(unit: _Unit, sep: str) -> _Unit:
    return replace(unit, sep=sep)


def _to_chunk(chunk_no: int, draft: _Draft, title: str) -> Chunk:
    pages = [u.page for u in draft.units if u.page is not None]
    context = " > ".join([title, *draft.heading_path]) if title else " > ".join(draft.heading_path)
    return Chunk(
        chunk_no=chunk_no,
        text=_join(draft.units),
        context=context,
        heading_path=draft.heading_path,
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
    )

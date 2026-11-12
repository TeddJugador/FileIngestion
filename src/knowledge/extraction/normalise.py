"""Text clean-up shared by every format."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# Spaces of every width (incl. no-break and zero-width) collapse to one ASCII space.
_SPACES = re.compile(r"[ \t\f\v\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000\ufeff]+")
_CONTROL = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f]")
# "pay-\nment" -> "payment": a hyphen at a line end followed by a lowercase letter.
_HYPHEN_BREAK = re.compile(r"(\w)-\n[ \t]*(?=[a-z])")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+\u2022]|\d{1,3}[.)])\s+")
_DIGITS = re.compile(r"\d+")


def clean_line(line: str) -> str:
    line = unicodedata.normalize("NFC", line)
    line = _CONTROL.sub("", line)
    return _SPACES.sub(" ", line).strip()


def fix_hyphenation(text: str) -> str:
    return _HYPHEN_BREAK.sub(r"\1", text)


def split_paragraphs(text: str) -> list[tuple[str, bool]]:
    """Paragraphs separated by blank lines, as ``(text, is_list)``.

    Lines inside a prose paragraph are joined with spaces (PDF line wraps); list items
    keep their own lines.
    """
    paragraphs: list[tuple[str, bool]] = []
    for raw in _BLANK_LINE.split(fix_hyphenation(text.replace("\r\n", "\n").replace("\r", "\n"))):
        lines = [clean_line(line) for line in raw.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            continue
        is_list = sum(bool(_LIST_ITEM.match(line)) for line in lines) >= max(1, len(lines) // 2)
        paragraphs.append(("\n".join(lines) if is_list else " ".join(lines), is_list))
    return paragraphs


MAX_NUMBERED_FURNITURE_CHARS = 25  # "Page 12 of 40", "- 3 -", "3/12"


def _line_keys(line: str) -> set[str]:
    """Keys under which a line counts as repeated. Exact text always; ignoring digits
    only for short lines, so 'Page 2 of 9' ~ 'Page 3 of 9' but 'Section 2. Learners must
    arrive by 07:30.' never matches 'Section 3. ...'."""
    cleaned = clean_line(line).lower()
    keys = {f"={cleaned}"}
    if len(cleaned) <= MAX_NUMBERED_FURNITURE_CHARS:
        keys.add(f"#{_DIGITS.sub('#', cleaned)}")
    return keys


def remove_repeated_lines(pages: list[list[str]], *, edge: int = 2) -> list[list[str]]:
    """Drop running headers and footers: lines in the first or last ``edge`` lines of a
    page that recur on more than half of the pages (page numbers may differ).

    Only page edges are considered, so a sentence a policy repeats in its body is kept.
    Needs at least 3 pages: with 2, "more than half" would mean any shared line.
    """
    if len(pages) < 3:
        return pages
    counts: Counter[str] = Counter()
    for lines in pages:
        edge_lines = [line for line in lines[:edge] + lines[-edge:] if line.strip()]
        counts.update({key for line in edge_lines for key in _line_keys(line)})
    repeated = {key for key, count in counts.items() if count > len(pages) / 2}
    cleaned = []
    for lines in pages:
        n = len(lines)
        cleaned.append(
            [
                line
                for i, line in enumerate(lines)
                if not ((i < edge or i >= n - edge) and _line_keys(line) & repeated)
            ]
        )
    return cleaned

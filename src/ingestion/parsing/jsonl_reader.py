from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import NoReturn

from ingestion.errors import FileRejected, ReasonCode
from ingestion.parsing.rows import RawRow


def _reject_constant(_name: str) -> NoReturn:
    # NaN / Infinity / -Infinity are not JSON, though Python's json module accepts them.
    raise ValueError("non-standard JSON constant")


def read_jsonl(lines: Iterable[bytes]) -> Iterator[RawRow]:
    """One JSON value per line; blank lines are skipped. ``RawRow.line`` is the
    physical line number. Floats are parsed as ``Decimal``."""
    decoded = codecs.iterdecode(lines, "utf-8-sig")
    try:
        for line_number, text in enumerate(decoded, start=1):
            if not text.strip():
                continue
            try:
                value = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
            except ValueError:  # JSONDecodeError subclasses ValueError
                # The decoder's message quotes the line; don't propagate it.
                raise FileRejected(
                    ReasonCode.UNREADABLE, f"malformed JSON on line {line_number}"
                ) from None
            yield RawRow(line=line_number, data=value)
    except UnicodeDecodeError:
        raise FileRejected(ReasonCode.UNREADABLE, "file is not valid UTF-8") from None

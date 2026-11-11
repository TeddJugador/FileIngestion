from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

import ijson

from ingestion.errors import FileRejected, ReasonCode
from ingestion.parsing.rows import RawRow


class Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def read_json(source: Readable) -> Iterator[RawRow]:
    """Stream the items of a top-level JSON array, one at a time, with ijson.

    Never loads the whole document. Numbers arrive as ``int`` or ``Decimal`` (ijson's
    default), never ``float``. ``RawRow.line`` is the 1-based array index.

    A well-formed document whose top level isn't an array is ``UNREADABLE``: ijson
    would otherwise yield nothing, misreporting it as ``NO_VALID_ROWS``. ijson error
    messages quote the input, so they are replaced with a fixed description.
    """
    try:
        events = ijson.parse(source)
        first = next(events, None)
        if first is None or first[1] != "start_array":
            raise FileRejected(ReasonCode.UNREADABLE, "JSON top level must be an array")
        items: Iterator[Any] = ijson.items(_prepend(first, events), "item")
        for index, item in enumerate(items, start=1):
            yield RawRow(line=index, data=item)
    except ijson.JSONError:
        raise FileRejected(ReasonCode.UNREADABLE, "malformed JSON") from None
    except UnicodeDecodeError:
        raise FileRejected(ReasonCode.UNREADABLE, "file is not valid UTF-8") from None


def _prepend(
    first: tuple[str, str, Any], rest: Iterator[tuple[str, str, Any]]
) -> Iterator[tuple[str, str, Any]]:
    yield first
    yield from rest

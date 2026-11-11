"""Byte-stream helpers that work on any iterable of chunks (e.g. ``iter_chunks()``)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

NEWLINE = b"\n"


def lines_from_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Split a chunked byte stream on ``\\n`` only, keeping line endings.

    Used instead of botocore's ``StreamingBody.iter_lines()``: that splits with
    ``bytes.splitlines``, so a ``\\r\\n`` that straddles a chunk boundary becomes two
    line breaks, which shifts every later line number and can corrupt a quoted CSV
    field. Splitting on ``\\n`` alone is safe for UTF-8 (0x0A never occurs inside a
    multi-byte character) and the csv module handles the remaining ``\\r``.
    """
    pending = b""
    for chunk in chunks:
        if not chunk:
            continue
        pending += chunk
        start = 0
        while (end := pending.find(NEWLINE, start)) != -1:
            yield pending[start : end + 1]
            start = end + 1
        pending = pending[start:]
    if pending:
        yield pending


class ChunkSource:
    """Minimal file-like ``read(size)`` over an iterable of chunks, for ijson.

    Holds at most one chunk plus a partial read in memory.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise ValueError("ChunkSource only supports bounded reads")
        while len(self._buffer) < size:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

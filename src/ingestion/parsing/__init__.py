"""Streaming readers for CSV, JSON (array) and JSONL uploads.

Readers yield ``RawRow``s and raise ``FileRejected`` for file-level problems
(``HEADER_MISMATCH``, ``UNREADABLE``). Rows already yielded stay valid, so the caller
can stop mid-file, abort its multipart upload and report counts so far.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ingestion.datasets import FileFormat
from ingestion.parsing.csv_reader import read_csv
from ingestion.parsing.json_reader import read_json
from ingestion.parsing.jsonl_reader import read_jsonl
from ingestion.parsing.rows import RawRow, RowError
from ingestion.parsing.streams import ChunkSource, lines_from_chunks

__all__ = ["ChunkSource", "RawRow", "RowError", "lines_from_chunks", "read_records"]


def read_records(
    file_format: FileFormat, chunks: Iterable[bytes], expected_columns: Iterable[str]
) -> Iterator[RawRow]:
    """Dispatch on format. ``chunks`` is e.g. ``StreamingBody.iter_chunks()``."""
    if file_format is FileFormat.CSV:
        return read_csv(lines_from_chunks(chunks), expected_columns)
    if file_format is FileFormat.JSONL:
        return read_jsonl(lines_from_chunks(chunks))
    return read_json(ChunkSource(chunks))

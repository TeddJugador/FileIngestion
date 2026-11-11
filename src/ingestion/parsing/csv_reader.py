from __future__ import annotations

import codecs
import csv
from collections import Counter
from collections.abc import Iterable, Iterator

from ingestion.errors import FileRejected, ReasonCode
from ingestion.parsing.rows import RawRow, RowError


def read_csv(lines: Iterable[bytes], expected_columns: Iterable[str]) -> Iterator[RawRow]:
    """Stream CSV rows as dicts, checking the header first.

    UTF-8 with optional BOM. The header must hold exactly the schema's columns, in any
    order (cell whitespace is ignored, names are case-sensitive). Short rows leave the
    missing columns absent, so validation reports ``missing``; long rows become a row
    error. Blank lines are skipped. ``RawRow.line`` is the physical line on which the
    record starts (header = line 1), which stays right for quoted multi-line fields.

    Uses ``csv.reader`` rather than ``DictReader`` because ``DictReader`` skips blank
    lines internally, which hides where the next record starts.
    """
    reader = csv.reader(codecs.iterdecode(lines, "utf-8-sig"))
    try:
        header = _next_non_blank(reader)
        if header is None:
            raise FileRejected(ReasonCode.HEADER_MISMATCH, "file has no header row")
        columns = [name.strip() for name in header]
        _check_header(columns, set(expected_columns))

        while True:
            start_line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                return
            if not cells:  # blank line
                continue
            yield _to_raw_row(columns, cells, start_line)
    except UnicodeDecodeError:
        raise FileRejected(ReasonCode.UNREADABLE, "file is not valid UTF-8") from None
    except csv.Error:
        # csv.Error messages can quote input; keep the detail content-free.
        raise FileRejected(ReasonCode.UNREADABLE, "malformed CSV") from None


def _next_non_blank(reader: Iterator[list[str]]) -> list[str] | None:
    for cells in reader:
        if cells:
            return cells
    return None


def _check_header(columns: list[str], expected: set[str]) -> None:
    duplicates = [name for name, count in Counter(columns).items() if count > 1]
    missing = sorted(expected - set(columns))
    unknown = set(columns) - expected
    if duplicates or missing or unknown:
        # Only schema column names are echoed; unknown header cells are counted, not
        # quoted, so the detail is safe to log.
        raise FileRejected(
            ReasonCode.HEADER_MISMATCH,
            f"missing columns {missing}; {len(unknown)} unknown and "
            f"{len(duplicates)} duplicated column name(s)",
        )


def _to_raw_row(columns: list[str], cells: list[str], line: int) -> RawRow:
    data = dict(zip(columns, cells, strict=False))  # short rows: trailing columns absent
    if len(cells) > len(columns):
        error = RowError(
            line=line,
            field="",
            error_code="too_many_cells",
            message="Row has more cells than the header",
            value="",
        )
        return RawRow(line=line, data=data, errors=(error,))
    return RawRow(line=line, data=data)

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from decimal import Decimal

import pytest

from ingestion.datasets import FileFormat
from ingestion.errors import FileRejected, ReasonCode
from ingestion.parsing import RawRow, lines_from_chunks, read_records
from ingestion.parsing.json_reader import read_json
from ingestion.parsing.streams import ChunkSource
from ingestion.schemas.marks import MarksRecord
from ingestion.schemas.roster import RosterRecord
from tests.unit.conftest import chunked, fixture_bytes

MARKS_COLUMNS = list(MarksRecord.model_fields)
ROSTER_COLUMNS = list(RosterRecord.model_fields)
MARKS_HEADER = b"student_number,assessment_code,term_code,mark,max_mark\n"
ROW = b"STU-2026-00412,T3,2026-T4,40,50\n"


def _csv(data: bytes, columns: list[str] = MARKS_COLUMNS) -> list[RawRow]:
    return list(read_records(FileFormat.CSV, chunked(data), columns))


def _json(data: bytes) -> list[RawRow]:
    return list(read_records(FileFormat.JSON, chunked(data), MARKS_COLUMNS))


def _jsonl(data: bytes) -> list[RawRow]:
    return list(read_records(FileFormat.JSONL, chunked(data), MARKS_COLUMNS))


def _reason(rows: Iterator[RawRow] | list[RawRow], reason: ReasonCode) -> FileRejected:
    with pytest.raises(FileRejected) as excinfo:
        list(rows)
    assert excinfo.value.reason is reason
    return excinfo.value


# --- line splitting -------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64])
def test_lines_from_chunks_splits_only_on_newline(size: int) -> None:
    data = b"a,b\r\nc,d\r\n\r\ne\rf\nlast"
    assert list(lines_from_chunks(chunked(data, size))) == [
        b"a,b\r\n",
        b"c,d\r\n",
        b"\r\n",
        b"e\rf\n",
        b"last",
    ]


def test_chunk_source_never_reads_unbounded() -> None:
    source = ChunkSource(iter([b"abc", b"def"]))
    assert source.read(4) == b"abcd"
    assert source.read(10) == b"ef"
    assert source.read(1) == b""
    with pytest.raises(ValueError, match="bounded"):
        source.read()


# --- CSV ----------------------------------------------------------------------------------


def test_csv_fixture_rows_and_line_numbers() -> None:
    rows = _csv(fixture_bytes("roster_valid.csv"), ROSTER_COLUMNS)
    assert [r.line for r in rows] == [2, 3, 4]
    assert rows[1].data["last_name"] == "van der Merwe"


def test_csv_strips_bom_and_accepts_any_column_order() -> None:
    data = (
        "\ufeffmax_mark,mark,term_code,assessment_code,student_number\n"
        "50,40,2026-T4,T3,STU-2026-00412\n"
    )
    (row,) = _csv(data.encode())
    assert row.data == {
        "max_mark": "50",
        "mark": "40",
        "term_code": "2026-T4",
        "assessment_code": "T3",
        "student_number": "STU-2026-00412",
    }


def test_csv_header_whitespace_is_ignored() -> None:
    data = b" student_number , assessment_code,term_code,mark,max_mark\r\n" + ROW
    (row,) = _csv(data)
    assert "student_number" in row.data


@pytest.mark.parametrize(
    "header",
    [
        b"student_number,assessment_code,term_code,mark\n",  # missing
        b"student_number,assessment_code,term_code,mark,max_mark,teacher\n",  # unknown
        b"student_number,assessment_code,term_code,mark,max_mark,mark\n",  # duplicate
        b"Student_Number,assessment_code,term_code,mark,max_mark\n",  # case-sensitive
    ],
)
def test_csv_header_mismatch(header: bytes) -> None:
    _reason(
        read_records(FileFormat.CSV, chunked(header + ROW), MARKS_COLUMNS),
        ReasonCode.HEADER_MISMATCH,
    )


def test_header_mismatch_detail_does_not_quote_unknown_columns() -> None:
    rejected = _reason(
        read_records(
            FileFormat.CSV, chunked(fixture_bytes("marks_header_mismatch.csv")), MARKS_COLUMNS
        ),
        ReasonCode.HEADER_MISMATCH,
    )
    assert "teacher" not in rejected.detail
    assert "assessment_code" in rejected.detail  # schema column names are fine to name


@pytest.mark.parametrize("data", [b"", b"\n\n", b"\xef\xbb\xbf"])
def test_csv_without_header_is_a_header_mismatch(data: bytes) -> None:
    _reason(read_records(FileFormat.CSV, chunked(data), MARKS_COLUMNS), ReasonCode.HEADER_MISMATCH)


def test_csv_header_only_yields_no_rows() -> None:
    assert _csv(fixture_bytes("marks_header_only.csv")) == []


def test_csv_blank_lines_are_skipped_and_line_numbers_stay_physical() -> None:
    rows = _csv(MARKS_HEADER + b"\n" + ROW + b"\r\n\n" + ROW)
    assert [r.line for r in rows] == [3, 6]


def test_csv_multiline_quoted_field_keeps_start_line() -> None:
    data = MARKS_HEADER + b'STU-2026-00412,"Essay\nPart 2",2026-T4,40,50\n' + ROW
    rows = _csv(data)
    assert [r.line for r in rows] == [2, 4]
    assert rows[0].data["assessment_code"] == "Essay\nPart 2"


def test_csv_short_row_leaves_columns_absent() -> None:
    (row,) = _csv(MARKS_HEADER + b"STU-2026-00412,T3,2026-T4\n")
    assert set(row.data) == {"student_number", "assessment_code", "term_code"}
    assert not row.errors


def test_csv_long_row_is_a_row_error() -> None:
    (row,) = _csv(MARKS_HEADER + b"STU-2026-00412,T3,2026-T4,40,50,extra\n")
    (error,) = row.errors
    assert (error.line, error.error_code, error.value) == (2, "too_many_cells", "")


def test_csv_invalid_utf8_midway_yields_earlier_rows_then_unreadable() -> None:
    rows = read_records(
        FileFormat.CSV, chunked(MARKS_HEADER + ROW + b"\xff\xfe,bad\n"), MARKS_COLUMNS
    )
    assert next(rows).line == 2
    _reason(rows, ReasonCode.UNREADABLE)


def test_csv_utf8_split_across_chunks_is_fine() -> None:
    data = (MARKS_HEADER.decode() + "STU-2026-00412,Hōkūlea ✓ café,2026-T4,40,50\n").encode()
    (row,) = list(read_records(FileFormat.CSV, chunked(data, 1), MARKS_COLUMNS))
    assert row.data["assessment_code"] == "Hōkūlea ✓ café"


def test_csv_nul_byte_is_a_value_not_a_file_failure() -> None:
    # Python 3.11+ csv accepts NUL; the value then fails schema validation as a row error.
    (row,) = _csv(MARKS_HEADER + b"STU\x00,T3,2026-T4,40,50\n")
    assert row.data["student_number"] == "STU\x00"


def test_csv_error_is_unreadable_without_quoting_input() -> None:
    huge_field = b"x" * 200_000  # above csv.field_size_limit() (128 KiB)
    rejected = _reason(
        read_records(
            FileFormat.CSV, chunked(MARKS_HEADER + huge_field + b",a,b,c,d\n", 4096), MARKS_COLUMNS
        ),
        ReasonCode.UNREADABLE,
    )
    assert rejected.detail == "malformed CSV"


# --- JSON ---------------------------------------------------------------------------------


def test_json_fixture_streams_items_with_index_as_line() -> None:
    rows = _json(fixture_bytes("marks_valid.json"))
    assert [r.line for r in rows] == [1, 2]
    assert rows[0].data["mark"] == Decimal("42.5")
    assert isinstance(rows[0].data["mark"], Decimal)


def test_json_empty_array_yields_nothing() -> None:
    assert _json(b"  [ ] ") == []


def test_json_malformed_midway_yields_earlier_rows_then_unreadable() -> None:
    rows = read_records(
        FileFormat.JSON, chunked(fixture_bytes("marks_malformed.json")), MARKS_COLUMNS
    )
    assert next(rows).line == 1
    rejected = _reason(rows, ReasonCode.UNREADABLE)
    assert rejected.detail == "malformed JSON"  # ijson's own message quotes the input


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"student_number": "STU-2026-00412"}',  # well-formed, but not an array
        b'"just a string"',
        b"[1, 2] trailing",
        b'[{"a": "\xff"}]',  # invalid UTF-8 inside a string
        b"[NaN]",
    ],
)
def test_json_unreadable(data: bytes) -> None:
    rejected = _reason(
        read_records(FileFormat.JSON, chunked(data), MARKS_COLUMNS), ReasonCode.UNREADABLE
    )
    assert rejected.detail in {"malformed JSON", "JSON top level must be an array"}


def test_json_non_object_items_reach_validation() -> None:
    rows = _json(b'[{"a": 1}, 5, "x", null]')
    assert [r.data for r in rows] == [{"a": 1}, 5, "x", None]


class _BoundedReader:
    """Fails if anything tries to slurp the whole body (json.load / read())."""

    def __init__(self, data: bytes) -> None:
        self._inner = io.BytesIO(data)
        self.max_request = 0
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("unbounded read")
        self.reads += 1
        self.max_request = max(self.max_request, size)
        return self._inner.read(size)


def test_json_is_read_incrementally() -> None:
    item = (
        b'{"student_number": "STU-2026-00412", "assessment_code": "T3",'
        b' "term_code": "2026-T4", "mark": 1, "max_mark": 2}'
    )
    data = b"[" + b",".join([item] * 5000) + b"]"  # ~560 KB
    reader = _BoundedReader(data)
    count = sum(1 for _ in read_json(reader))
    assert count == 5000
    assert reader.max_request <= 64 * 1024  # ijson's buffer size, not the file size
    assert reader.reads >= len(data) // (64 * 1024)


# --- JSONL --------------------------------------------------------------------------------


def test_jsonl_fixture_skips_blank_lines_and_keeps_physical_line_numbers() -> None:
    rows = _jsonl(fixture_bytes("marks_valid.jsonl"))
    assert [r.line for r in rows] == [1, 3]
    assert isinstance(rows[0].data["mark"], Decimal)


def test_jsonl_crlf_and_bom() -> None:
    rows = _jsonl(b'\xef\xbb\xbf{"a": 1}\r\n{"a": 2.5}\r\n')
    assert [r.data for r in rows] == [{"a": 1}, {"a": Decimal("2.5")}]


@pytest.mark.parametrize("bad", [b'{"a": 1', b"NaN", b'{"a": Infinity}', b"\xff"])
def test_jsonl_bad_line_is_unreadable_after_earlier_rows(bad: bytes) -> None:
    rows = read_records(
        FileFormat.JSONL, chunked(b'{"a": 1}\n' + bad + b'\n{"a": 3}\n'), MARKS_COLUMNS
    )
    assert next(rows).line == 1
    rejected = _reason(rows, ReasonCode.UNREADABLE)
    assert "a" not in rejected.detail.replace("malformed", "").replace("valid", "").replace(
        "line", ""
    )


def test_jsonl_empty_file_yields_nothing() -> None:
    assert _jsonl(b"") == []
    assert _jsonl(b"\n  \n") == []


def test_jsonl_valid_json_that_is_not_an_object_reaches_validation() -> None:
    assert [r.data for r in _jsonl(b'[1]\n"x"\n')] == [[1], "x"]


# --- end to end: parse + validate --------------------------------------------------------


def test_mixed_roster_file_gives_row_errors_with_line_numbers() -> None:
    from datetime import date

    from ingestion.schemas.common import ValidationContext
    from ingestion.schemas.validation import ValidRow, validate_row

    context = ValidationContext(as_of=date(2026, 11, 11))
    rows: Iterable[RawRow] = read_records(
        FileFormat.CSV, chunked(fixture_bytes("roster_mixed.csv")), ROSTER_COLUMNS
    )
    results = [validate_row(RosterRecord, row, context) for row in rows]
    assert isinstance(results[0], ValidRow)
    errors = [e for r in results if isinstance(r, list) for e in r]
    assert {(e.line, e.field, e.error_code) for e in errors} == {
        (3, "student_number", "string_pattern_mismatch"),
        (4, "class_code", "class_grade_mismatch"),
        (5, "date_of_birth", "date_format"),
        (5, "guardian_email", "value_error"),
        (5, "guardian_phone", "string_pattern_mismatch"),
    }

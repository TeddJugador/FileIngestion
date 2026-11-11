from __future__ import annotations

import pytest

from ingestion import datasets
from ingestion.datasets import (
    DATASETS,
    DatasetKind,
    FileFormat,
    RejectedKey,
    UploadKey,
    dataset_for_path_name,
    parse_upload_key,
)
from ingestion.errors import ReasonCode
from ingestion.schemas.fee_schedule import FeeScheduleRecord
from ingestion.schemas.marks import MarksRecord
from ingestion.schemas.roster import RosterRecord

# --- registry ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path_name", "event_name"),
    [
        ("roster", "ROSTER"),
        ("marks", "MARKS"),
        ("fee_schedule", "FEE_SCHEDULE"),
        ("documents", "DOCUMENTS"),
    ],
)
def test_path_name_maps_to_event_name(path_name: str, event_name: str) -> None:
    spec = dataset_for_path_name(path_name)
    assert spec is not None
    assert spec.event_name == event_name


def test_registry_holds_exactly_the_four_datasets() -> None:
    assert set(DATASETS) == {"roster", "marks", "fee_schedule", "documents"}


@pytest.mark.parametrize("name", ["MARKS", "Marks", "unknown", ""])
def test_lookup_is_exact_and_case_sensitive(name: str) -> None:
    assert dataset_for_path_name(name) is None


def test_tabular_and_document_extensions() -> None:
    for name in ("roster", "marks", "fee_schedule"):
        spec = DATASETS[name]
        assert spec.kind is DatasetKind.TABULAR
        assert spec.extensions == {"csv", "json", "jsonl"}
    assert DATASETS["documents"].kind is DatasetKind.DOCUMENT
    assert DATASETS["documents"].extensions == {"pdf", "docx", "md", "txt"}


def test_schemas_are_wired_to_the_right_datasets() -> None:
    assert DATASETS["roster"].schema is RosterRecord
    assert DATASETS["marks"].schema is MarksRecord
    assert DATASETS["fee_schedule"].schema is FeeScheduleRecord
    assert DATASETS["documents"].schema is None


def test_default_size_limits() -> None:
    assert datasets.ROSTER.max_bytes() == 20_971_520
    assert datasets.DOCUMENTS.max_bytes() == 26_214_400


def test_size_limits_follow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TABULAR_BYTES", "1000")
    monkeypatch.setenv("MAX_DOCUMENT_BYTES", "2000")
    assert datasets.MARKS.max_bytes() == 1000
    assert datasets.DOCUMENTS.max_bytes() == 2000


def test_natural_keys() -> None:
    roster = RosterRecord.model_validate(
        {
            "student_number": "STU-2026-00412",
            "first_name": "A",
            "last_name": "B",
            "date_of_birth": "2011-04-17",
            "grade_code": "10",
            "class_code": "10B",
            "guardian_email": "a@example.org",
            "guardian_phone": "+27821234567",
        }
    )
    marks = MarksRecord.model_validate(
        {
            "student_number": "STU-2026-00412",
            "assessment_code": "T3",
            "term_code": "2026-T4",
            "mark": "1",
            "max_mark": "2",
        }
    )
    fees = FeeScheduleRecord.model_validate(
        {
            "grade_code": "R",
            "term_code": "2027-T1",
            "fee_item": "Tuition",
            "amount_cents": "100",
            "currency": "ZAR",
        }
    )
    assert datasets.ROSTER.natural_key is not None
    assert datasets.MARKS.natural_key is not None
    assert datasets.FEE_SCHEDULE.natural_key is not None
    assert datasets.ROSTER.natural_key(roster) == "STU-2026-00412"
    assert datasets.MARKS.natural_key(marks) == "STU-2026-00412#T3#2026-T4"
    assert datasets.FEE_SCHEDULE.natural_key(fees) == "R#2027-T1#Tuition"


# --- key routing -----------------------------------------------------------------------


def _accepted(key: str) -> UploadKey:
    result = parse_upload_key(key)
    assert isinstance(result, UploadKey), result
    return result


def _rejected(key: str) -> RejectedKey:
    result = parse_upload_key(key)
    assert isinstance(result, RejectedKey), result
    assert result.reason is ReasonCode.UNSUPPORTED_FORMAT
    return result


@pytest.mark.parametrize(
    ("key", "path_name", "fmt"),
    [
        ("raw/marks/SCH-001/term4.csv", "marks", FileFormat.CSV),
        ("raw/roster/SCH-123/2027 intake.json", "roster", FileFormat.JSON),
        ("raw/fee_schedule/SCH-001/fees.v2.jsonl", "fee_schedule", FileFormat.JSONL),
    ],
)
def test_tabular_keys_route(key: str, path_name: str, fmt: FileFormat) -> None:
    upload = _accepted(key)
    assert upload.dataset.path_name == path_name
    assert upload.school_id == key.split("/")[2]
    assert upload.file_format is fmt


def test_document_key_keeps_subfolders_in_relative_path() -> None:
    upload = _accepted("raw/documents/SCH-001/policies/code_of_conduct.pdf")
    assert upload.dataset.kind is DatasetKind.DOCUMENT
    assert upload.relative_path == "policies/code_of_conduct.pdf"
    assert upload.extension == "pdf"
    assert upload.file_format is None


def test_unknown_dataset_is_rejected_but_keeps_a_valid_school_id() -> None:
    rejected = _rejected("raw/attendance/SCH-001/week1.csv")
    assert rejected.dataset is None
    assert rejected.school_id == "SCH-001"
    assert not rejected.components_trusted


@pytest.mark.parametrize("segment", ["MARKS", "Marks", "fee-schedule"])
def test_dataset_segment_must_be_exact_lowercase(segment: str) -> None:
    assert _rejected(f"raw/{segment}/SCH-001/a.csv").dataset is None


@pytest.mark.parametrize("school", ["SCH-1", "SCH-0001", "sch-001", "SCH_001", "..", ""])
def test_invalid_school_id_is_rejected(school: str) -> None:
    rejected = _rejected(f"raw/marks/{school}/a.csv")
    assert rejected.school_id is None
    assert rejected.dataset is not None
    assert not rejected.components_trusted


@pytest.mark.parametrize(
    "key",
    [
        "raw/marks/SCH-001/term4.pdf",  # document extension under tabular
        "raw/documents/SCH-001/policy.csv",  # tabular extension under documents
        "raw/marks/SCH-001/term4.CSV",  # extensions are case-sensitive
        "raw/marks/SCH-001/term4.xlsx",
        "raw/marks/SCH-001/term4",  # no extension
        "raw/marks/SCH-001/.csv",  # no file name
        "raw/marks/SCH-001/sub/term4.csv",  # tabular uploads have no subfolders
        "raw/documents/SCH-001//policy.pdf",  # empty segment
    ],
)
def test_bad_file_names_are_rejected_with_trusted_components(key: str) -> None:
    rejected = _rejected(key)
    assert rejected.components_trusted


@pytest.mark.parametrize("key", ["processed/marks/SCH-001/a.csv", "raw/marks", "raw/a.csv"])
def test_malformed_keys_are_rejected(key: str) -> None:
    _rejected(key)

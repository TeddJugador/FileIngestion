from __future__ import annotations

import json
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer

from ingestion.errors import ReasonCode
from ingestion.notifications import S3ObjectRef
from tests.unit.aws import BUCKET, UPLOADER, Harness
from tests.unit.conftest import fixture_bytes

_deserialize = TypeDeserializer().deserialize


def _item(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: _deserialize(v) for k, v in raw.items()}


def _prefix(dataset: str, ref: S3ObjectRef, school: str = "SCH-001") -> str:
    return f"processed/{dataset}/{school}/{ref.import_id}"


# --- happy path: every dataset, every format -----------------------------------------


@pytest.mark.parametrize(
    ("fixture", "dataset", "rows"),
    [
        ("roster_valid.csv", "roster", 3),
        ("roster_valid.jsonl", "roster", 2),
        ("marks_valid.csv", "marks", 3),
        ("marks_valid.json", "marks", 2),
        ("marks_valid.jsonl", "marks", 2),
        ("fee_schedule_valid.csv", "fee_schedule", 3),
        ("fee_schedule_valid.json", "fee_schedule", 2),
    ],
)
def test_valid_file_end_to_end(harness: Harness, fixture: str, dataset: str, rows: int) -> None:
    extension = fixture.rsplit(".", 1)[1]
    ref = harness.upload(f"raw/{dataset}/SCH-001/upload.{extension}", fixture_bytes(fixture))

    result = harness.pipeline().process(ref)

    assert result.status == "COMPLETED"
    assert (result.total_rows, result.valid_rows, result.invalid_rows) == (rows, rows, 0)

    prefix = _prefix(dataset, ref)
    assert len(harness.get_lines(f"{prefix}/valid.jsonl")) == rows

    summary = harness.get_json(f"{prefix}/summary.json")
    assert summary["status"] == "COMPLETED"
    assert summary["datasetType"] == dataset.upper()
    assert summary["sourceVersionId"] == ref.version_id
    assert "reasonCode" not in summary
    assert summary["errors"] == []

    items = [_item(i) for i in harness.items_for_import(ref.import_id)]
    assert len(items) == rows
    for item in items:
        assert item["PK"].startswith(f"SCHOOL#SCH-001#{dataset.upper()}#")
        assert item["SK"] == f"IMPORT#{ref.import_id}"
        assert item["source_version_id"] == ref.version_id
        assert len(item["record_hash"]) == 64
        assert item["ttl_timestamp"] > 0

    (event,) = harness.events.details("ImportCompleted")
    assert harness.events.details("ImportFailed") == []
    assert event["data"] == {
        "importId": ref.import_id,
        "datasetType": dataset.upper(),
        "sourceFileKey": ref.key,
        "sourceVersionId": ref.version_id,
        "status": "COMPLETED",
        "totalRows": rows,
        "validRows": rows,
        "invalidRows": 0,
        "validRecordsKey": f"{prefix}/valid.jsonl",
        "summaryKey": f"{prefix}/summary.json",
        "uploadedBy": UPLOADER,
    }
    metadata = event["metadata"]
    assert metadata["correlationId"] == ref.import_id
    assert metadata["schoolId"] == "SCH-001"
    assert metadata["producer"] == "ingestion-service"
    assert metadata["schemaVersion"] == "1.0"
    assert len(metadata["eventId"]) == 26  # ULID
    assert metadata["occurredAt"].endswith("Z")
    (entry,) = harness.events.entries
    assert entry["Source"] == "school.ingestion"
    assert entry["EventBusName"] == "SchoolEventBus"


def test_marks_are_numbers_in_dynamodb_and_strings_in_jsonl(harness: Harness) -> None:
    ref = harness.upload("raw/marks/SCH-001/t4.csv", fixture_bytes("marks_valid.csv"))
    harness.pipeline().process(ref)
    items = {i["PK"]["S"]: i for i in harness.items_for_import(ref.import_id)}
    item = items["SCHOOL#SCH-001#MARKS#STU-2026-00412#MATHS-TEST-3#2026-T4"]
    assert item["mark"] == {"N": "42.5"}
    line = harness.get_lines(f"{_prefix('marks', ref)}/valid.jsonl")[0]
    assert line["mark"] == "42.5"  # no floats, ever


def test_ttl_is_30_days_after_ingestion(harness: Harness) -> None:
    from datetime import UTC, datetime, timedelta

    fixed = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    ref = harness.upload("raw/marks/SCH-001/t4.csv", fixture_bytes("marks_valid.csv"))
    pipeline = harness.pipeline()
    pipeline._s = pipeline._s.__class__(**{**pipeline._s.__dict__, "now": lambda: fixed})
    pipeline.process(ref)
    item = _item(harness.items_for_import(ref.import_id)[0])
    assert item["ttl_timestamp"] == int((fixed + timedelta(days=30)).timestamp())
    assert item["ingested_at"] == fixed.isoformat()


# --- row-level errors -------------------------------------------------------------------


def test_mixed_rows_complete_with_errors(harness: Harness) -> None:
    ref = harness.upload("raw/roster/SCH-001/intake.csv", fixture_bytes("roster_mixed.csv"))
    result = harness.pipeline().process(ref)

    assert result.status == "COMPLETED_WITH_ERRORS"
    assert (result.total_rows, result.valid_rows, result.invalid_rows) == (4, 1, 3)
    summary = harness.get_json(f"{_prefix('roster', ref)}/summary.json")
    assert {(e["line"], e["field"], e["errorCode"]) for e in summary["errors"]} == {
        (3, "student_number", "string_pattern_mismatch"),
        (4, "class_code", "class_grade_mismatch"),
        (5, "date_of_birth", "date_format"),
        (5, "guardian_email", "value_error"),
        (5, "guardian_phone", "string_pattern_mismatch"),
    }
    (event,) = harness.events.details("ImportCompleted")
    assert event["data"]["status"] == "COMPLETED_WITH_ERRORS"

    # Error values and record contents never reach the logs.
    logs = harness.logs()
    assert "import completed" in logs
    for secret in ("STU-26-412", "not-an-email", "17/03/2011", "0821234567", "Mokoena", "Dlamini"):
        assert secret not in logs
    assert "class_grade_mismatch" in logs  # codes are fine


def test_json_extra_field_is_a_row_error(harness: Harness) -> None:
    rows = [
        {
            "student_number": "STU-2026-00412",
            "assessment_code": "T3",
            "term_code": "2026-T4",
            "mark": 40,
            "max_mark": 50,
        },
        {
            "student_number": "STU-2026-00413",
            "assessment_code": "T3",
            "term_code": "2026-T4",
            "mark": 40,
            "max_mark": 50,
            "teacher": "Ms Naidoo",
        },
    ]
    ref = harness.upload("raw/marks/SCH-001/t.json", json.dumps(rows).encode())
    result = harness.pipeline().process(ref)
    assert result.status == "COMPLETED_WITH_ERRORS"
    summary = harness.get_json(f"{_prefix('marks', ref)}/summary.json")
    assert [(e["line"], e["field"], e["errorCode"]) for e in summary["errors"]] == [
        (2, "teacher", "extra_forbidden")
    ]


def test_duplicate_natural_key_keeps_the_first_row(harness: Harness) -> None:
    body = (
        b"student_number,assessment_code,term_code,mark,max_mark\n"
        b"STU-2026-00412,T3,2026-T4,40,50\n"
        b"STU-2026-00412,T3,2026-T4,45,50\n"
    )
    ref = harness.upload("raw/marks/SCH-001/dup.csv", body)
    result = harness.pipeline().process(ref)
    assert (result.valid_rows, result.invalid_rows) == (1, 1)
    summary = harness.get_json(f"{_prefix('marks', ref)}/summary.json")
    assert [(e["line"], e["errorCode"]) for e in summary["errors"]] == [(3, "duplicate_key")]
    (item,) = harness.items_for_import(ref.import_id)
    assert item["mark"] == {"N": "40"}


def test_only_first_1000_errors_are_detailed(harness: Harness) -> None:
    header = b"student_number,assessment_code,term_code,mark,max_mark\n"
    bad = b"".join(f"STU-2026-{i:05d},T3,2026-T4,99,50\n".encode() for i in range(1200))
    good = b"STU-2026-99999,T3,2026-T4,40,50\n"
    ref = harness.upload("raw/marks/SCH-001/many.csv", header + bad + good)
    harness.pipeline().process(ref)
    summary = harness.get_json(f"{_prefix('marks', ref)}/summary.json")
    assert summary["invalidRows"] == 1200
    assert len(summary["errors"]) == 1000
    assert summary["errorsTruncated"] is True


# --- file-level failures --------------------------------------------------------------


def _failed(harness: Harness) -> dict[str, Any]:
    assert harness.events.details("ImportCompleted") == []
    (event,) = harness.events.details("ImportFailed")
    return event


@pytest.mark.parametrize(
    ("key", "body_fixture", "reason"),
    [
        ("raw/marks/SCH-001/t.csv", "marks_header_mismatch.csv", ReasonCode.HEADER_MISMATCH),
        ("raw/attendance/SCH-001/t.csv", "marks_valid.csv", ReasonCode.UNSUPPORTED_FORMAT),
        ("raw/marks/SCH-001/t.pdf", "marks_valid.csv", ReasonCode.UNSUPPORTED_FORMAT),
        ("raw/documents/SCH-001/t.csv", "marks_valid.csv", ReasonCode.UNSUPPORTED_FORMAT),
        ("raw/marks/SCH-001/t.csv", "marks_header_only.csv", ReasonCode.NO_VALID_ROWS),
        ("raw/marks/SCH-001/t.json", "marks_malformed.json", ReasonCode.UNREADABLE),
    ],
)
def test_file_level_failures(
    harness: Harness, key: str, body_fixture: str, reason: ReasonCode
) -> None:
    ref = harness.upload(key, fixture_bytes(body_fixture))
    result = harness.pipeline().process(ref)  # returns normally: a business outcome
    assert result.status == "FAILED"
    assert result.reason_code == reason
    event = _failed(harness)
    assert event["data"]["reasonCode"] == reason
    assert event["data"]["uploadedBy"] in (UPLOADER, None)
    assert harness.get_json(event["data"]["summaryKey"])["reasonCode"] == reason
    assert not any(k.endswith("valid.jsonl") for k in harness.object_keys())
    assert harness.metrics.value("ImportsFailedCount") == 1


def test_unknown_dataset_reports_unknown_and_goes_to_rejected(harness: Harness) -> None:
    ref = harness.upload("raw/attendance/SCH-001/week1.csv", b"a,b\n1,2\n")
    harness.pipeline().process(ref)
    event = _failed(harness)
    assert event["data"]["datasetType"] == "UNKNOWN"
    assert event["metadata"]["schoolId"] == "SCH-001"
    assert event["data"]["summaryKey"] == f"processed/_rejected/{ref.import_id}/summary.json"


def test_invalid_school_id_goes_to_rejected_with_unknown_school(harness: Harness) -> None:
    ref = harness.upload("raw/marks/SCH-01/t.csv", fixture_bytes("marks_valid.csv"))
    harness.pipeline().process(ref)
    event = _failed(harness)
    assert event["metadata"]["schoolId"] == "UNKNOWN"
    assert event["data"]["datasetType"] == "MARKS"
    assert event["data"]["summaryKey"] == f"processed/_rejected/{ref.import_id}/summary.json"
    assert harness.object_keys() == [f"processed/_rejected/{ref.import_id}/summary.json"]
    assert harness.get_json(event["data"]["summaryKey"])["schoolId"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("dataset", "extension", "env", "limit"),
    [
        ("marks", "csv", "MAX_TABULAR_BYTES", 100),
        ("roster", "json", "MAX_TABULAR_BYTES", 100),
        ("documents", "pdf", "MAX_DOCUMENT_BYTES", 200),
    ],
)
def test_oversized_file_per_dataset_limit(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    dataset: str,
    extension: str,
    env: str,
    limit: int,
) -> None:
    monkeypatch.setenv(env, str(limit))
    ok = harness.upload(
        f"raw/{dataset}/SCH-001/ok.{extension}", b"x" * limit, metadata={"audience": "STAFF"}
    )
    big = harness.upload(
        f"raw/{dataset}/SCH-001/big.{extension}", b"x" * (limit + 1), metadata={"audience": "STAFF"}
    )
    assert harness.pipeline().process(big).reason_code == ReasonCode.FILE_TOO_LARGE
    assert harness.pipeline().process(ok).reason_code != ReasonCode.FILE_TOO_LARGE


@pytest.mark.parametrize("uploaded_by", [None, "", "   ", "x" * 129])
def test_missing_uploaded_by(harness: Harness, uploaded_by: str | None) -> None:
    ref = harness.upload(
        "raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"), uploaded_by=uploaded_by
    )
    assert harness.pipeline().process(ref).reason_code == ReasonCode.MISSING_METADATA
    assert _failed(harness)["data"]["uploadedBy"] is None


def test_every_row_invalid_is_no_valid_rows_with_errors_listed(harness: Harness) -> None:
    body = b"student_number,assessment_code,term_code,mark,max_mark\nBAD,T3,2026-T4,40,50\n"
    ref = harness.upload("raw/marks/SCH-001/t.csv", body)
    result = harness.pipeline().process(ref)
    assert result.reason_code == ReasonCode.NO_VALID_ROWS
    summary = harness.get_json(f"{_prefix('marks', ref)}/summary.json")
    assert summary["status"] == "FAILED"
    assert (summary["totalRows"], summary["validRows"], summary["invalidRows"]) == (1, 0, 1)
    assert summary["errors"][0]["errorCode"] == "string_pattern_mismatch"
    assert not harness.exists(f"{_prefix('marks', ref)}/valid.jsonl")


def test_malformed_json_midway_reports_counts_and_leaves_no_upload_open(harness: Harness) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.json", fixture_bytes("marks_malformed.json"))
    result = harness.pipeline().process(ref)
    assert result.reason_code == ReasonCode.UNREADABLE
    assert result.valid_rows == 1  # the row before the break
    assert harness.open_multipart_uploads() == []
    assert not harness.exists(f"{_prefix('marks', ref)}/valid.jsonl")


@pytest.mark.slow
def test_malformed_json_after_a_part_was_uploaded_aborts_the_upload(harness: Harness) -> None:
    item = (
        '{{"student_number": "STU-2026-{i:05d}", "assessment_code": "ESSAY-{j}",'
        ' "term_code": "2026-T4", "mark": 40, "max_mark": 50}}'
    )
    rows = ",".join(item.format(i=i % 100_000, j=i // 100_000) for i in range(75_000))
    ref = harness.upload("raw/marks/SCH-001/big.json", f"[{rows}, {{oops".encode())
    result = harness.pipeline().process(ref)
    assert result.reason_code == ReasonCode.UNREADABLE
    assert result.valid_rows == 75_000  # > 5 MiB of output, so a part was uploaded
    assert harness.open_multipart_uploads() == []
    assert not harness.exists(f"{_prefix('marks', ref)}/valid.jsonl")


def test_deleted_version_is_unreadable(harness: Harness) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    harness.s3.delete_object(Bucket=BUCKET, Key=ref.key, VersionId=ref.version_id)
    assert harness.pipeline().process(ref).reason_code == ReasonCode.UNREADABLE


def test_key_with_spaces_and_special_characters(harness: Harness) -> None:
    ref = harness.upload(
        "raw/marks/SCH-001/Term 4 + Maths (Gr 10) & más.csv", fixture_bytes("marks_valid.csv")
    )
    result = harness.pipeline().process(ref)
    assert result.status == "COMPLETED"
    (event,) = harness.events.details("ImportCompleted")
    assert event["data"]["sourceFileKey"] == "raw/marks/SCH-001/Term 4 + Maths (Gr 10) & más.csv"


# --- ordering and failure propagation -----------------------------------------------------


class _OrderCheckingPublisher:
    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self.published: list[str] = []

    def publish(self, detail_type: Any, **kwargs: Any) -> str:
        data = kwargs["data"]
        # The claim-check files must already exist when the event goes out.
        assert self._harness.exists(data["summaryKey"])
        if "validRecordsKey" in data:
            assert self._harness.exists(data["validRecordsKey"])
        self.published.append(str(detail_type))
        return "01J0000000000000000000000"


@pytest.mark.parametrize(
    ("fixture", "detail_type"),
    [("marks_valid.csv", "ImportCompleted"), ("marks_header_only.csv", "ImportFailed")],
)
def test_event_is_published_after_claim_check_files_exist(
    harness: Harness, fixture: str, detail_type: str
) -> None:
    publisher = _OrderCheckingPublisher(harness)
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes(fixture))
    harness.pipeline(publisher=publisher).process(ref)
    assert publisher.published == [detail_type]


def test_failed_put_events_entry_raises(harness: Harness) -> None:
    from ingestion.events import EventPublisher, EventPublishError

    class FailingEvents:
        def put_events(self, **_: Any) -> dict[str, Any]:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}

    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_valid.csv"))
    with pytest.raises(EventPublishError):
        harness.pipeline(publisher=EventPublisher(FailingEvents(), "bus")).process(ref)  # type: ignore[arg-type]


def test_metrics(harness: Harness) -> None:
    ref = harness.upload("raw/roster/SCH-001/intake.csv", fixture_bytes("roster_mixed.csv"))
    harness.pipeline().process(ref)
    assert harness.metrics.value("ProcessedRecordsCount") == 4
    assert harness.metrics.value("ValidRecordsCount") == 1
    assert harness.metrics.value("ValidationFailureCount") == 3
    latency = [r for r in harness.metrics.records if r[0] == "PipelineLatency"]
    assert len(latency) == 1
    assert latency[0][2] == "Milliseconds"
    assert all(r[3]["DatasetType"] == "roster" for r in harness.metrics.records)


def test_failure_metric_has_reason_dimension(harness: Harness) -> None:
    ref = harness.upload("raw/marks/SCH-001/t.csv", fixture_bytes("marks_header_mismatch.csv"))
    harness.pipeline().process(ref)
    (failed,) = [r for r in harness.metrics.records if r[0] == "ImportsFailedCount"]
    assert failed[3] == {"DatasetType": "marks", "ReasonCode": "HEADER_MISMATCH"}

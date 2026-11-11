from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ingestion.notifications import S3ObjectRef, VersioningDisabledError, parse_notification


def _body(key: str, version_id: str | None = "v1") -> str:
    obj = {"key": key, "size": 123, "eTag": "abc"}
    if version_id is not None:
        obj["versionId"] = version_id
    return json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "eventTime": "2026-11-11T08:00:00.123Z",
                    "s3": {"bucket": {"name": "b"}, "object": obj},
                }
            ]
        }
    )


def test_test_event_is_ignored() -> None:
    assert parse_notification(json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"})) == []


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        ("raw/marks/SCH-001/term+4.csv", "raw/marks/SCH-001/term 4.csv"),
        ("raw/marks/SCH-001/a%2Bb.csv", "raw/marks/SCH-001/a+b.csv"),
        ("raw/marks/SCH-001/caf%C3%A9+%26+co.csv", "raw/marks/SCH-001/café & co.csv"),
    ],
)
def test_keys_are_url_decoded(encoded: str, decoded: str) -> None:
    (ref,) = parse_notification(_body(encoded))
    assert ref.key == decoded


def test_fields() -> None:
    (ref,) = parse_notification(_body("raw/marks/SCH-001/t.csv"))
    assert (ref.bucket, ref.version_id, ref.size, ref.etag) == ("b", "v1", 123, "abc")
    assert ref.event_time == datetime(2026, 11, 11, 8, 0, 0, 123000, tzinfo=UTC)


def test_missing_version_id_raises() -> None:
    with pytest.raises(VersioningDisabledError):
        parse_notification(_body("raw/marks/SCH-001/t.csv", version_id=None))


def test_malformed_envelope_raises() -> None:
    with pytest.raises((KeyError, ValueError)):
        parse_notification('{"not": "an s3 event"}')


def test_import_id_depends_on_version_not_content() -> None:
    def ref(version: str) -> S3ObjectRef:
        return S3ObjectRef(
            "b", "raw/marks/SCH-001/t.csv", version, 1, "same-etag", datetime.now(UTC)
        )

    assert ref("v1").import_id == ref("v1").import_id
    assert ref("v1").import_id != ref("v2").import_id
    assert ref("v1").import_id.startswith("imp-")
    assert len(ref("v1").import_id) == 16


def test_payload_round_trip() -> None:
    (ref,) = parse_notification(_body("raw/marks/SCH-001/t.csv"))
    assert S3ObjectRef.from_payload(json.loads(json.dumps(ref.to_payload()))) == ref

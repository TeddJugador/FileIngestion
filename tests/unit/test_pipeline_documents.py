from __future__ import annotations

import io
import zipfile

import pytest

from ingestion.errors import ReasonCode
from tests.unit.aws import UPLOADER, Harness

PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer\n%%EOF\n"
KEY = "raw/documents/SCH-001/policies/code_of_conduct.pdf"


def _docx(word: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if word:
            archive.writestr("word/document.xml", "<w:document/>")
        else:
            archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


def test_valid_pdf_is_staged_and_announced(harness: Harness) -> None:
    ref = harness.upload(
        KEY, PDF, metadata={"audience": "PARENT, STAFF", "title": "Learner Code of Conduct 2027"}
    )
    result = harness.pipeline().process(ref)
    assert result.status == "ACCEPTED"

    (event,) = harness.events.details("DocumentAccepted")
    data = event["data"]
    assert data["documentId"].startswith("doc-")
    assert data["version"] == "v-" + data["sha256"][:12]
    assert data["audience"] == ["STAFF", "PARENT"]
    assert data["title"] == "Learner Code of Conduct 2027"
    assert data["contentType"] == "application/pdf"
    assert data["sizeBytes"] == len(PDF)
    assert data["importId"] == ref.import_id
    assert data["sourceVersionId"] == ref.version_id
    assert data["uploadedBy"] == UPLOADER
    assert (
        data["stagedKey"]
        == f"processed/documents/SCH-001/{data['documentId']}/{data['version']}/source.pdf"
    )
    assert harness.s3.get_object(Bucket=ref.bucket, Key=data["stagedKey"])["Body"].read() == PDF

    summary_key = data["stagedKey"].rsplit("/", 1)[0] + "/summary.json"
    assert harness.get_json(summary_key)["status"] == "ACCEPTED"
    assert harness.events.details("ImportFailed") == []
    assert harness.metrics.value("DocumentsAcceptedCount") == 1


def test_reupload_keeps_document_id_and_changes_version(harness: Harness) -> None:
    first = harness.upload(KEY, PDF, metadata={"audience": "STAFF"})
    second = harness.upload(KEY, PDF + b"% revised\n", metadata={"audience": "STAFF"})
    pipeline = harness.pipeline()
    pipeline.process(first)
    pipeline.process(second)
    a, b = harness.events.details("DocumentAccepted")
    assert a["data"]["documentId"] == b["data"]["documentId"]
    assert a["data"]["version"] != b["data"]["version"]
    assert a["data"]["importId"] != b["data"]["importId"]


def test_title_defaults_to_file_name(harness: Harness) -> None:
    ref = harness.upload(KEY, PDF, metadata={"audience": "STAFF"})
    harness.pipeline().process(ref)
    (event,) = harness.events.details("DocumentAccepted")
    assert event["data"]["title"] == "code_of_conduct"


@pytest.mark.parametrize("audience", [None, "", "TEACHER", "STAFF,PUPIL", "staff", "STAFF,"])
def test_missing_or_invalid_audience(harness: Harness, audience: str | None) -> None:
    metadata = {} if audience is None else {"audience": audience}
    ref = harness.upload(KEY, PDF, metadata=metadata)
    assert harness.pipeline().process(ref).reason_code == ReasonCode.MISSING_METADATA
    (event,) = harness.events.details("ImportFailed")
    assert event["data"]["datasetType"] == "DOCUMENTS"


@pytest.mark.parametrize(
    ("key", "body"),
    [
        ("raw/documents/SCH-001/fake.pdf", b"just some text pretending to be a pdf"),
        ("raw/documents/SCH-001/sheet.docx", _docx(word=False)),  # a ZIP, but not Word
        ("raw/documents/SCH-001/notzip.docx", b"PK\x03\x04 not really a zip"),
        ("raw/documents/SCH-001/notes.md", b"caf\xe9 latin-1, not UTF-8"),
    ],
)
def test_content_must_match_extension(harness: Harness, key: str, body: bytes) -> None:
    ref = harness.upload(key, body, metadata={"audience": "STAFF"})
    assert harness.pipeline().process(ref).reason_code == ReasonCode.UNSUPPORTED_FORMAT
    assert not [k for k in harness.object_keys() if "/source." in k]


@pytest.mark.parametrize(
    ("name", "body", "content_type"),
    [
        (
            "handbook.docx",
            _docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("faq.md", "# FAQ\n\nUniform: navy blue — café hours 07:30\n".encode(), "text/markdown"),
        ("notes.txt", b"\xef\xbb\xbfplain text with BOM", "text/plain"),
    ],
)
def test_other_document_types(harness: Harness, name: str, body: bytes, content_type: str) -> None:
    ref = harness.upload(f"raw/documents/SCH-001/{name}", body, metadata={"audience": "PARENT"})
    assert harness.pipeline().process(ref).status == "ACCEPTED"
    (event,) = harness.events.details("DocumentAccepted")
    assert event["data"]["contentType"] == content_type


def test_staged_copy_is_the_hashed_version_even_after_an_overwrite(harness: Harness) -> None:
    ref = harness.upload(KEY, PDF, metadata={"audience": "STAFF"})
    harness.upload(KEY, b"%PDF-1.7 newer content", metadata={"audience": "STAFF"})
    harness.pipeline().process(ref)  # process the older version
    (event,) = harness.events.details("DocumentAccepted")
    assert (
        harness.s3.get_object(Bucket=ref.bucket, Key=event["data"]["stagedKey"])["Body"].read()
        == PDF
    )

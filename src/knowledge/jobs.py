"""The two message shapes on document-indexing-queue, parsed into one ``IndexJob``.

- ``DocumentAccepted``: an EventBridge event from ingestion (the queue body is the whole
  event). Also how events EventBridge couldn't deliver look when redriven from the DLQ.
- ``REINDEX``: an internal message from admin_ops; the source is already in the
  knowledge bucket.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

SchoolId = Annotated[str, StringConstraints(pattern=r"^SCH-\d{3}$")]
DocumentId = Annotated[str, StringConstraints(pattern=r"^doc-[0-9a-f]{12}$")]
Version = Annotated[str, StringConstraints(pattern=r"^v-[0-9a-f]{12}$")]
Audience = Literal["STAFF", "PARENT"]
EXTENSIONS = ("pdf", "docx", "md", "txt")


class InvalidJobError(ValueError):
    """Not a message this queue should carry. Raised so it ends up in the DLQ."""


class JobKind(StrEnum):
    ACCEPTED = "ACCEPTED"
    REINDEX = "REINDEX"


@dataclass(frozen=True)
class IndexJob:
    kind: JobKind
    school_id: str
    document_id: str
    version: str
    import_id: str
    title: str
    audience: tuple[str, ...]
    accepted_at: str = ""  # ISO time ingestion accepted the upload (orders versions)
    staged_key: str = ""  # ACCEPTED: ingestion bucket key of the staged copy
    sha256: str = ""  # ACCEPTED: expected hash of the source
    source_key: str = ""  # REINDEX: knowledge bucket key of this service's copy

    @property
    def correlation_id(self) -> str:
        return f"{self.document_id}#{self.version}"

    @property
    def extension(self) -> str:
        key = self.staged_key or self.source_key
        return key.rsplit(".", 1)[-1].lower()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = str(self.kind)
        payload["audience"] = list(self.audience)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IndexJob:
        return cls(
            **{**payload, "kind": JobKind(payload["kind"]), "audience": tuple(payload["audience"])}
        )


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _AcceptedData(_Model):
    documentId: DocumentId
    version: Version
    importId: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    audience: Annotated[list[Audience], Field(min_length=1)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    stagedKey: str


class _Metadata(_Model):
    schoolId: SchoolId
    occurredAt: str


class _Detail(_Model):
    metadata: _Metadata
    data: _AcceptedData


class _Event(_Model):
    source: Literal["school.ingestion"]
    detail_type: Literal["DocumentAccepted"] = Field(alias="detail-type")
    detail: _Detail


class _Reindex(_Model):
    kind: Literal["REINDEX"]
    schoolId: SchoolId
    documentId: DocumentId
    version: Version
    sourceKey: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    audience: Annotated[list[Audience], Field(min_length=1)]
    importId: str


def parse_job(body: str) -> IndexJob:
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("kind") == "REINDEX":
            message = _Reindex.model_validate(payload)
            job = IndexJob(
                kind=JobKind.REINDEX,
                school_id=message.schoolId,
                document_id=message.documentId,
                version=message.version,
                import_id=message.importId,
                title=message.title,
                audience=tuple(message.audience),
                source_key=message.sourceKey,
            )
        else:
            event = _Event.model_validate(payload)
            data, metadata = event.detail.data, event.detail.metadata
            job = IndexJob(
                kind=JobKind.ACCEPTED,
                school_id=metadata.schoolId,
                document_id=data.documentId,
                version=data.version,
                import_id=data.importId,
                title=data.title,
                audience=tuple(data.audience),
                accepted_at=metadata.occurredAt,
                staged_key=data.stagedKey,
                sha256=data.sha256,
            )
    except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
        raise InvalidJobError(f"unrecognised indexing message ({type(exc).__name__})") from None
    if job.extension not in EXTENSIONS:
        raise InvalidJobError("unsupported document extension")
    return job


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

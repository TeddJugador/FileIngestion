"""Parse the S3 event notification carried in an SQS message body."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote_plus


class VersioningDisabledError(Exception):
    """A notification without ``versionId``: bucket versioning is off (deployment fault)."""


@dataclass(frozen=True)
class S3ObjectRef:
    bucket: str
    key: str  # URL-decoded
    version_id: str
    size: int
    etag: str
    event_time: datetime

    @property
    def import_id(self) -> str:
        """Same object version -> same import; a re-upload is a new version -> new import."""
        digest = hashlib.sha256(f"{self.bucket}/{self.key}/{self.version_id}".encode())
        return f"imp-{digest.hexdigest()[:12]}"

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable form; ``import_id`` is the idempotency key."""
        return {
            "import_id": self.import_id,
            "bucket": self.bucket,
            "key": self.key,
            "version_id": self.version_id,
            "size": self.size,
            "etag": self.etag,
            "event_time": self.event_time.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> S3ObjectRef:
        return cls(
            bucket=payload["bucket"],
            key=payload["key"],
            version_id=payload["version_id"],
            size=int(payload["size"]),
            etag=payload["etag"],
            event_time=datetime.fromisoformat(payload["event_time"]),
        )


def parse_notification(body: str) -> list[S3ObjectRef]:
    """Return the objects in an S3 notification; ``[]`` for ``s3:TestEvent``.

    Anything malformed raises, so the message is retried and ends up in the DLQ.
    """
    payload = json.loads(body)
    if payload.get("Event") == "s3:TestEvent":
        return []
    refs = []
    for record in payload["Records"]:
        obj = record["s3"]["object"]
        version_id = obj.get("versionId")
        if not version_id:
            raise VersioningDisabledError("S3 notification has no versionId")
        refs.append(
            S3ObjectRef(
                bucket=record["s3"]["bucket"]["name"],
                key=unquote_plus(obj["key"]),  # S3 encodes spaces as "+"
                version_id=version_id,
                size=int(obj["size"]),
                etag=obj.get("eTag", ""),
                event_time=datetime.fromisoformat(record["eventTime"].replace("Z", "+00:00")),
            )
        )
    return refs

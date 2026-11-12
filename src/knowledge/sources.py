"""This service's own document copies and vector-key lists in the knowledge bucket."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

STAGED_PREFIX = "processed/documents/"
READ_CHUNK = 1024 * 1024
MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied"})


class SourceUnavailableError(Exception):
    """The staged copy is gone (ingestion expires it after 45 days) and this service has
    no copy of its own: the admin must re-upload. Permanent."""


class SourceTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class Downloaded:
    data: bytes
    sha256: str


def source_key(school_id: str, document_id: str, version: str, extension: str) -> str:
    return f"sources/{school_id}/{document_id}/{version}/source.{extension}"


def vector_keys_key(school_id: str, document_id: str, version: str) -> str:
    return f"vector-keys/{school_id}/{document_id}/{version}.json"


class SourceStore:
    def __init__(
        self, s3: S3Client, *, knowledge_bucket: str, ingestion_bucket: str, max_bytes: int
    ) -> None:
        self._s3 = s3
        self._bucket = knowledge_bucket
        self._ingestion = ingestion_bucket
        self._max_bytes = max_bytes

    def copy_from_staging(self, staged_key: str, destination_key: str) -> None:
        """Server-side copy from ingestion's staging area into the knowledge bucket.

        Without s3:ListBucket on the ingestion bucket (deliberately not granted), S3
        answers a missing object with 403 rather than 404; the grant covers exactly
        processed/documents/*, so either code here means the staged copy is gone.
        """
        try:
            self._s3.copy_object(
                Bucket=self._bucket,
                Key=destination_key,
                CopySource={"Bucket": self._ingestion, "Key": staged_key},
                MetadataDirective="COPY",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in MISSING_CODES:
                raise SourceUnavailableError("staged copy not found") from None
            raise

    def download(self, key: str) -> Downloaded:
        """Stream to /tmp (spec), enforcing the size cap, then read back once hashed."""
        try:
            body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"]
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in MISSING_CODES:
                raise SourceUnavailableError("knowledge copy not found") from None
            raise
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=0, dir="/tmp") as spool:
            for chunk in body.iter_chunks(READ_CHUNK):
                size += len(chunk)
                if size > self._max_bytes:
                    raise SourceTooLargeError(f"source exceeds {self._max_bytes} bytes")
                digest.update(chunk)
                spool.write(chunk)
            body.close()
            spool.seek(0)
            return Downloaded(data=spool.read(), sha256=digest.hexdigest())

    def put_vector_keys(self, key: str, vector_keys: list[str]) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(vector_keys).encode(),
            ContentType="application/json",
        )

    def get_vector_keys(self, key: str) -> list[str]:
        try:
            body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in MISSING_CODES:
                return []
            raise
        keys = json.loads(body)
        return [str(k) for k in keys]

    def delete(self, key: str) -> None:
        if key:
            self._s3.delete_object(Bucket=self._bucket, Key=key)

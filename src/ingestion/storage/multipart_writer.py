"""Stream JSON lines to one S3 object without holding the whole file in memory."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import CompletedPartTypeDef

MIN_PART_SIZE = 5 * 1024 * 1024  # S3: every part except the last must be >= 5 MiB


class JsonlMultipartWriter:
    """Buffers lines and uploads 5 MiB parts; use as a context manager.

    - The multipart upload is created lazily, on the first full part.
    - ``close()``: nothing written -> no object; no part uploaded yet -> one
      ``put_object``; otherwise upload the remainder as the last part and complete.
    - Leaving the ``with`` block without a successful ``close()`` (any exception,
      including ``FileRejected``) aborts the multipart upload. The bucket's 1-day
      abort-incomplete-multipart lifecycle rule is the backstop.

    Peak memory is about one part (``part_size`` plus one line).
    """

    def __init__(
        self,
        s3: S3Client,
        bucket: str,
        key: str,
        *,
        part_size: int = MIN_PART_SIZE,
        content_type: str = "application/x-ndjson",
    ) -> None:
        if part_size < MIN_PART_SIZE:
            raise ValueError("part_size must be at least 5 MiB")
        self._s3 = s3
        self._bucket = bucket
        self._key = key
        self._part_size = part_size
        self._content_type = content_type
        self._buffer = bytearray()
        self._upload_id: str | None = None
        self._parts: list[CompletedPartTypeDef] = []
        self._closed = False
        self.bytes_written = 0
        self.lines_written = 0

    @property
    def key(self) -> str:
        return self._key

    @property
    def part_count(self) -> int:
        return len(self._parts)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._closed:
            self.abort()

    def write_line(self, line: bytes) -> None:
        if self._closed:
            raise ValueError("writer is closed")
        self._buffer += line
        self._buffer += b"\n"
        self.bytes_written += len(line) + 1
        self.lines_written += 1
        if len(self._buffer) >= self._part_size:
            self._upload_part(bytes(self._buffer))
            self._buffer.clear()

    def close(self) -> bool:
        """Finish the object. Returns ``True`` if an object was written."""
        if self._closed:
            raise ValueError("writer is closed")
        if self.bytes_written == 0:
            self._closed = True
            return False
        if self._upload_id is None:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=bytes(self._buffer),
                ContentType=self._content_type,
            )
        else:
            if self._buffer:
                self._upload_part(bytes(self._buffer))
            self._s3.complete_multipart_upload(
                Bucket=self._bucket,
                Key=self._key,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        self._buffer.clear()
        self._closed = True
        return True

    def abort(self) -> None:
        self._closed = True
        self._buffer.clear()
        if self._upload_id is not None:
            upload_id, self._upload_id = self._upload_id, None
            self._s3.abort_multipart_upload(Bucket=self._bucket, Key=self._key, UploadId=upload_id)

    def _upload_part(self, data: bytes) -> None:
        if self._upload_id is None:
            created = self._s3.create_multipart_upload(
                Bucket=self._bucket, Key=self._key, ContentType=self._content_type
            )
            self._upload_id = created["UploadId"]
        part_number = len(self._parts) + 1
        uploaded = self._s3.upload_part(
            Bucket=self._bucket,
            Key=self._key,
            UploadId=self._upload_id,
            PartNumber=part_number,
            Body=data,
        )
        self._parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})

"""Per-file processing: route, pre-check, validate/stage, report, publish.

Everything here is plain Python with injected clients, so it runs under moto without
Lambda. The handler wraps ``Pipeline.process`` in Powertools idempotency.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from aws_lambda_powertools import Logger
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from pydantic import BaseModel

from ingestion import documents
from ingestion.clock import utc_now
from ingestion.datasets import (
    UNKNOWN,
    DatasetKind,
    DatasetSpec,
    RejectedKey,
    UploadKey,
    parse_upload_key,
)
from ingestion.errors import FileRejected, ReasonCode
from ingestion.events import DetailType, EventPublisher
from ingestion.notifications import S3ObjectRef
from ingestion.parsing import read_records
from ingestion.parsing.rows import RowError
from ingestion.reporting import ErrorCollector, Summary, write_summary
from ingestion.schemas.common import ValidationContext
from ingestion.schemas.validation import validate_row
from ingestion.storage import BatchWriter, JsonlMultipartWriter

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_s3 import S3Client

PROCESSED_PREFIX = "processed"
RECORD_TTL = timedelta(days=30)
READ_CHUNK_BYTES = 256 * 1024
SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024
MAX_UPLOADED_BY_CHARS = 128
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})

_serializer = TypeSerializer()


class MetricsSink(Protocol):
    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class Services:
    s3: S3Client
    dynamodb: DynamoDBClient
    publisher: EventPublisher
    metrics: MetricsSink
    logger: Logger
    records_table: str
    now: Callable[[], datetime] = utc_now


@dataclass(frozen=True)
class FileResult:
    """Small, JSON-serialisable result stored by the idempotency layer."""

    import_id: str
    status: str
    reason_code: str | None = None
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "importId": self.import_id,
            "status": self.status,
            "reasonCode": self.reason_code,
            "totalRows": self.total_rows,
            "validRows": self.valid_rows,
            "invalidRows": self.invalid_rows,
        }


@dataclass
class _Head:
    found: bool
    size: int
    metadata: dict[str, str]


class Pipeline:
    def __init__(self, services: Services) -> None:
        self._s = services

    # --- entry point -------------------------------------------------------------

    def process(self, ref: S3ObjectRef) -> FileResult:
        started = self._s.now()
        self._s.logger.append_keys(import_id=ref.import_id)

        routed = parse_upload_key(ref.key)
        if isinstance(routed, RejectedKey):
            return self._fail(ref, started, routed.reason, routed.dataset, routed.school_id, None)

        self._s.logger.append_keys(
            school_id=routed.school_id, dataset_type=routed.dataset.path_name
        )
        head = self._head(ref)
        dataset, school = routed.dataset, routed.school_id
        uploaded_by = _uploaded_by(head.metadata)

        size = head.size if head.found else ref.size
        if size > dataset.max_bytes():
            return self._fail(ref, started, ReasonCode.FILE_TOO_LARGE, dataset, school, uploaded_by)
        if not head.found:  # the version was deleted before we got to it
            return self._fail(ref, started, ReasonCode.UNREADABLE, dataset, school, uploaded_by)
        if uploaded_by is None:
            return self._fail(ref, started, ReasonCode.MISSING_METADATA, dataset, school, None)

        if dataset.kind is DatasetKind.DOCUMENT:
            audience = documents.parse_audience(head.metadata.get("audience"))
            if audience is None:
                return self._fail(
                    ref, started, ReasonCode.MISSING_METADATA, dataset, school, uploaded_by
                )
            return self._process_document(ref, routed, head, audience, uploaded_by, started)
        return self._process_tabular(ref, routed, uploaded_by, started)

    # --- tabular ------------------------------------------------------------------

    def _process_tabular(
        self, ref: S3ObjectRef, routed: UploadKey, uploaded_by: str, started: datetime
    ) -> FileResult:
        dataset = routed.dataset
        schema, natural_key, file_format = dataset.schema, dataset.natural_key, routed.file_format
        assert schema is not None
        assert natural_key is not None
        assert file_format is not None

        prefix = _import_prefix(dataset, routed.school_id, ref.import_id)
        context = ValidationContext(as_of=ref.event_time.date())
        errors = ErrorCollector()
        seen_keys: set[str] = set()
        total = valid = 0
        ingested_at = started
        items = BatchWriter(self._s.dynamodb, self._s.records_table)

        body = self._s.s3.get_object(Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id)[
            "Body"
        ]
        try:
            with JsonlMultipartWriter(
                self._s.s3, ref.bucket, f"{prefix}/valid.jsonl"
            ) as valid_file:
                rows = read_records(
                    file_format, body.iter_chunks(READ_CHUNK_BYTES), schema.model_fields
                )
                for row in rows:
                    total += 1
                    result = validate_row(schema, row, context)
                    if isinstance(result, list):
                        errors.add(result)
                        continue
                    key = natural_key(result.record)
                    if key in seen_keys:
                        errors.add([_duplicate_error(row.line)])
                        continue
                    seen_keys.add(key)
                    canonical = _canonical_json(result.record)
                    items.put(
                        _item(
                            dataset,
                            routed.school_id,
                            key,
                            result.record,
                            canonical,
                            ref,
                            ingested_at,
                        )
                    )
                    valid_file.write_line(canonical.encode())
                    valid += 1
                items.flush()
                valid_file.close()
        except FileRejected as rejected:
            # The writer has already aborted its multipart upload. Rows flushed to
            # DynamoDB stay until TTL; no ImportCompleted is published, so no consumer
            # reads them.
            self._s.logger.warning(
                "file rejected while reading",
                reason_code=str(rejected.reason),
                detail=rejected.detail,
            )
            return self._fail(
                ref,
                started,
                rejected.reason,
                dataset,
                routed.school_id,
                uploaded_by,
                counts=(total, valid),
                errors=errors,
            )
        finally:
            body.close()

        if valid == 0:
            return self._fail(
                ref,
                started,
                ReasonCode.NO_VALID_ROWS,
                dataset,
                routed.school_id,
                uploaded_by,
                counts=(total, valid),
                errors=errors,
            )

        invalid = total - valid
        status = "COMPLETED" if invalid == 0 else "COMPLETED_WITH_ERRORS"
        summary_key = f"{prefix}/summary.json"
        finished = self._s.now()
        write_summary(
            self._s.s3,
            ref.bucket,
            summary_key,
            Summary(
                import_id=ref.import_id,
                source_file_key=ref.key,
                source_version_id=ref.version_id,
                dataset_type=dataset.event_name,
                school_id=routed.school_id,
                status=status,
                started_at=started,
                finished_at=finished,
                total_rows=total,
                valid_rows=valid,
                invalid_rows=invalid,
                errors=errors,
            ),
        )
        # Publish last: the claim-check files above now exist.
        self._s.publisher.publish(
            DetailType.IMPORT_COMPLETED,
            school_id=routed.school_id,
            correlation_id=ref.import_id,
            occurred_at=finished,
            data={
                "importId": ref.import_id,
                "datasetType": dataset.event_name,
                "sourceFileKey": ref.key,
                "sourceVersionId": ref.version_id,
                "status": status,
                "totalRows": total,
                "validRows": valid,
                "invalidRows": invalid,
                "validRecordsKey": f"{prefix}/valid.jsonl",
                "summaryKey": summary_key,
                "uploadedBy": uploaded_by,
            },
        )
        dims = {"DatasetType": dataset.path_name}
        self._s.metrics.record("ProcessedRecordsCount", total, "Count", dims)
        self._s.metrics.record("ValidRecordsCount", valid, "Count", dims)
        self._s.metrics.record("ValidationFailureCount", invalid, "Count", dims)
        self._record_latency(ref, dims)
        self._s.logger.info(
            "import completed",
            status=status,
            total_rows=total,
            valid_rows=valid,
            invalid_rows=invalid,
            error_count=errors.total,
            error_codes=errors.codes(),  # codes only, never values
        )
        return FileResult(ref.import_id, status, None, total, valid, invalid)

    # --- documents --------------------------------------------------------------

    def _process_document(
        self,
        ref: S3ObjectRef,
        routed: UploadKey,
        head: _Head,
        audience: list[str],
        uploaded_by: str,
        started: datetime,
    ) -> FileResult:
        dataset, school = routed.dataset, routed.school_id
        limit = dataset.max_bytes()
        body = self._s.s3.get_object(Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id)[
            "Body"
        ]
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY_BYTES) as spool:
            try:
                for chunk in body.iter_chunks(READ_CHUNK_BYTES):
                    size += len(chunk)
                    if size > limit:  # defensive: HEAD already said it fits
                        return self._fail(
                            ref, started, ReasonCode.FILE_TOO_LARGE, dataset, school, uploaded_by
                        )
                    digest.update(chunk)
                    spool.write(chunk)
            finally:
                body.close()
            if not documents.content_matches_extension(routed.extension, spool):  # type: ignore[arg-type]
                return self._fail(
                    ref, started, ReasonCode.UNSUPPORTED_FORMAT, dataset, school, uploaded_by
                )

        sha256 = digest.hexdigest()
        doc_id = documents.document_id(school, routed.relative_path)
        version = documents.document_version(sha256)
        doc_prefix = f"{PROCESSED_PREFIX}/{dataset.path_name}/{school}/{doc_id}/{version}"
        staged_key = f"{doc_prefix}/source.{routed.extension}"
        content_type = documents.CONTENT_TYPES[routed.extension]
        title = documents.resolve_title(head.metadata.get("title"), routed.relative_path)

        # Copy the exact version we hashed, so a concurrent overwrite of the key can't
        # make the hash and the staged copy disagree.
        self._s.s3.copy_object(
            Bucket=ref.bucket,
            Key=staged_key,
            CopySource={"Bucket": ref.bucket, "Key": ref.key, "VersionId": ref.version_id},
            ContentType=content_type,
            MetadataDirective="REPLACE",
        )
        summary_key = f"{doc_prefix}/summary.json"
        finished = self._s.now()
        write_summary(
            self._s.s3,
            ref.bucket,
            summary_key,
            Summary(
                import_id=ref.import_id,
                source_file_key=ref.key,
                source_version_id=ref.version_id,
                dataset_type=dataset.event_name,
                school_id=school,
                status="ACCEPTED",
                started_at=started,
                finished_at=finished,
                extra={
                    "documentId": doc_id,
                    "version": version,
                    "sha256": sha256,
                    "sizeBytes": size,
                    "stagedKey": staged_key,
                },
            ),
        )
        self._s.publisher.publish(
            DetailType.DOCUMENT_ACCEPTED,
            school_id=school,
            correlation_id=ref.import_id,
            occurred_at=finished,
            data={
                "documentId": doc_id,
                "version": version,
                "importId": ref.import_id,
                "title": title,
                "audience": audience,
                "contentType": content_type,
                "sizeBytes": size,
                "sha256": sha256,
                "sourceFileKey": ref.key,
                "sourceVersionId": ref.version_id,
                "stagedKey": staged_key,
                "uploadedBy": uploaded_by,
            },
        )
        dims = {"DatasetType": dataset.path_name}
        self._s.metrics.record("DocumentsAcceptedCount", 1, "Count", dims)
        self._record_latency(ref, dims)
        self._s.logger.info(
            "document accepted", document_id=doc_id, version=version, size_bytes=size
        )
        return FileResult(ref.import_id, "ACCEPTED")

    # --- failures ---------------------------------------------------------------

    def _fail(
        self,
        ref: S3ObjectRef,
        started: datetime,
        reason: ReasonCode,
        dataset: DatasetSpec | None,
        school_id: str | None,
        uploaded_by: str | None,
        *,
        counts: tuple[int, int] = (0, 0),
        errors: ErrorCollector | None = None,
    ) -> FileResult:
        """Write the summary, publish ``ImportFailed`` and return success.

        A bad file is a business outcome, not a retryable error.
        """
        total, valid = counts
        dataset_name = dataset.event_name if dataset else UNKNOWN
        school = school_id or UNKNOWN
        if dataset is not None and school_id is not None:
            summary_key = f"{_import_prefix(dataset, school_id, ref.import_id)}/summary.json"
        else:
            # Key components can't be trusted to build a path: never echo them.
            summary_key = f"{PROCESSED_PREFIX}/_rejected/{ref.import_id}/summary.json"

        finished = self._s.now()
        write_summary(
            self._s.s3,
            ref.bucket,
            summary_key,
            Summary(
                import_id=ref.import_id,
                source_file_key=ref.key,
                source_version_id=ref.version_id,
                dataset_type=dataset_name,
                school_id=school,
                status="FAILED",
                reason_code=str(reason),
                started_at=started,
                finished_at=finished,
                total_rows=total,
                valid_rows=valid,
                invalid_rows=total - valid,
                errors=errors or ErrorCollector(),
            ),
        )
        self._s.publisher.publish(
            DetailType.IMPORT_FAILED,
            school_id=school,
            correlation_id=ref.import_id,
            occurred_at=finished,
            data={
                "importId": ref.import_id,
                "datasetType": dataset_name,
                "sourceFileKey": ref.key,
                "sourceVersionId": ref.version_id,
                "reasonCode": str(reason),
                "summaryKey": summary_key,
                "uploadedBy": uploaded_by,
            },
        )
        dims = {"DatasetType": dataset.path_name if dataset else "unknown"}
        self._s.metrics.record(
            "ImportsFailedCount", 1, "Count", {**dims, "ReasonCode": str(reason)}
        )
        if total:
            self._s.metrics.record("ProcessedRecordsCount", total, "Count", dims)
            self._s.metrics.record("ValidationFailureCount", total - valid, "Count", dims)
        self._record_latency(ref, dims)
        self._s.logger.info(
            "import failed",
            reason_code=str(reason),
            total_rows=total,
            valid_rows=valid,
            error_codes=errors.codes() if errors else [],
        )
        return FileResult(ref.import_id, "FAILED", str(reason), total, valid, total - valid)

    # --- helpers ----------------------------------------------------------------

    def _head(self, ref: S3ObjectRef) -> _Head:
        try:
            response = self._s.s3.head_object(
                Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in NOT_FOUND_CODES:
                return _Head(found=False, size=ref.size, metadata={})
            raise
        return _Head(
            found=True, size=response["ContentLength"], metadata=response.get("Metadata", {})
        )

    def _record_latency(self, ref: S3ObjectRef, dims: dict[str, str]) -> None:
        latency_ms = (self._s.now() - ref.event_time).total_seconds() * 1000
        self._s.metrics.record("PipelineLatency", max(latency_ms, 0.0), "Milliseconds", dims)


def _import_prefix(dataset: DatasetSpec, school_id: str, import_id: str) -> str:
    return f"{PROCESSED_PREFIX}/{dataset.path_name}/{school_id}/{import_id}"


def _uploaded_by(metadata: dict[str, str]) -> str | None:
    value = metadata.get("uploaded-by", "").strip()
    if not value or len(value) > MAX_UPLOADED_BY_CHARS:
        return None
    return value


def _duplicate_error(line: int) -> RowError:
    return RowError(
        line=line,
        field="",
        error_code="duplicate_key",
        message="Another row in this file has the same natural key; the first one was kept",
        value="",
    )


def _canonical_json(record: BaseModel) -> str:
    """Sorted keys, no whitespace. Decimals serialise as strings (never floats)."""
    return json.dumps(
        record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _item(
    dataset: DatasetSpec,
    school_id: str,
    natural_key: str,
    record: BaseModel,
    canonical: str,
    ref: S3ObjectRef,
    ingested_at: datetime,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        name: _attribute(value) for name, value in record.model_dump().items()
    }
    attributes.update(
        {
            "PK": f"SCHOOL#{school_id}#{dataset.event_name}#{natural_key}",
            "SK": f"IMPORT#{ref.import_id}",
            "import_id": ref.import_id,
            "ingested_at": ingested_at.isoformat(),
            "source_file_key": ref.key,
            "source_version_id": ref.version_id,
            "record_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "ttl_timestamp": int((ingested_at + RECORD_TTL).timestamp()),
        }
    )
    return {name: _serializer.serialize(value) for name, value in attributes.items()}


def _attribute(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):  # schemas never produce floats; guard anyway
        return Decimal(str(value))
    return value

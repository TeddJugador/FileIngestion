"""Indexing one document version (spec section 5).

Plain Python with injected dependencies, so tests run it with moto, an in-memory vector
store and the fake embedder. The handler adds SQS batching and idempotency.

Where this departs from the spec's step order, the reason is noted inline:
- The vector-key list is written *before* the vectors (spec: after), so a crash or
  timeout mid-write still leaves a list that names every vector to clean up.
- Whether this service already has the source is read from the registry's
  ``source_key``, not from a HEAD request: without s3:ListBucket, S3 answers HEAD on a
  missing object with 403, which can't be told apart from a real permission error.
- Out-of-order completion is decided by when ingestion *accepted* each upload, not by
  ``indexed_at``: the version that finishes last is not necessarily the newer upload.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from aws_lambda_powertools import Logger

from knowledge.chunking import Chunk, ChunkingError, chunk_document
from knowledge.events import DetailType, Publisher
from knowledge.extraction import ExtractionError, extract
from knowledge.jobs import IndexJob, JobKind, parse_time
from knowledge.providers.base import Embedder
from knowledge.providers.errors import ProviderError
from knowledge.registry import ActivationConflictError, Registry, Status, VersionRecord
from knowledge.sources import (
    STAGED_PREFIX,
    SourceStore,
    SourceTooLargeError,
    SourceUnavailableError,
    source_key,
    vector_keys_key,
)
from knowledge.tokens import estimate_tokens
from knowledge.vector_metadata import MetadataTooLargeError, build_metadata, vector_key
from knowledge.vector_store import VectorRecord, VectorStore, VectorStoreError
from knowledge.versions import VersionCleaner


class Outcome(StrEnum):
    INDEXED = "INDEXED"
    REINDEXED = "REINDEXED"
    SKIPPED = "SKIPPED"  # already active in this index, or no longer active (reindex)
    SUPERSEDED = "SUPERSEDED"  # a newer upload of the same document won the race
    FAILED = "FAILED"  # permanent failure, reported with DocumentIndexingFailed


class FailureReason(StrEnum):
    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    EMBEDDING_PROVIDER_ERROR = "EMBEDDING_PROVIDER_ERROR"
    VECTOR_WRITE_FAILED = "VECTOR_WRITE_FAILED"


class PermanentFailure(Exception):
    def __init__(self, reason: FailureReason, log_reason: str) -> None:
        super().__init__(f"{reason}: {log_reason}")
        self.reason = reason
        self.log_reason = log_reason


class MetricsSink(Protocol):
    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class IndexResult:
    outcome: Outcome
    chunk_count: int = 0
    reason: str = ""
    replaced_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "chunkCount": self.chunk_count,
            "reason": self.reason,
            "replacedVersion": self.replaced_version,
        }


@dataclass(frozen=True)
class IndexerSettings:
    index_name: str
    embedding_model_id: str
    embedding_dimension: int
    max_receive_count: int = 3


@dataclass
class Indexer:
    settings: IndexerSettings
    registry: Registry
    sources: SourceStore
    store: VectorStore
    embedder: Embedder
    publisher: Publisher
    metrics: MetricsSink
    logger: Logger
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    # --- entry point --------------------------------------------------------------------

    def process(self, job: IndexJob, *, receive_count: int = 1) -> IndexResult:
        started = time.monotonic()
        self.logger.append_keys(
            school_id=job.school_id,
            document_id=job.document_id,
            version=job.version,
            kind=str(job.kind),
        )
        existing = self.registry.get_version(job.school_id, job.document_id, job.version)

        # Step 1: nothing to do if this version is already live in the configured index.
        if (
            existing
            and existing.status == Status.ACTIVE
            and existing.index_name == self.settings.index_name
        ):
            self.logger.info("version already active in this index")
            return IndexResult(Outcome.SKIPPED, existing.chunk_count, "already_active")
        if job.kind is JobKind.REINDEX and (existing is None or existing.status != Status.ACTIVE):
            self.logger.info("reindex skipped: version is no longer active")
            return IndexResult(Outcome.SKIPPED, reason="not_active")

        document = self.registry.get_document(job.school_id, job.document_id)
        previous = document.active_version if document else None
        if job.kind is JobKind.ACCEPTED:  # step 2
            self.registry.start_indexing(
                school_id=job.school_id,
                document_id=job.document_id,
                version=job.version,
                import_id=job.import_id,
                accepted_at=job.accepted_at,
                title=job.title,
                audience=job.audience,
                index_name=self.settings.index_name,
                now=self.now(),
            )

        planned_keys: list[str] = []
        written_keys: list[str] = []
        try:
            source = self._take_source(job, existing)  # step 3
            chunks = self._chunks(job, source)  # steps 3-5
            planned_keys = [vector_key(job.document_id, job.version, c.chunk_no) for c in chunks]
            list_key = vector_keys_key(job.school_id, job.document_id, job.version)
            if job.kind is JobKind.ACCEPTED:
                # Step 8, moved before step 7: a list that names every vector of this
                # version exists before any vector does.
                self.sources.put_vector_keys(list_key, planned_keys)
                self.registry.set_version_fields(
                    job.school_id, job.document_id, job.version, {"vector_keys_key": list_key}
                )
            tokens = self._embed_and_write(job, chunks, written_keys)  # steps 6-7
        except PermanentFailure as failure:
            return self._fail_permanently(job, failure, planned_keys, written_keys)
        except Exception as exc:
            self._fail_retryably(job, exc, written_keys, receive_count)
            raise

        if job.kind is JobKind.REINDEX:
            return self._finish_reindex(job, list_key, planned_keys, tokens, started)
        return self._finish_index(job, previous, list_key, len(chunks), tokens, started)

    # --- steps --------------------------------------------------------------------------

    def _take_source(self, job: IndexJob, existing: VersionRecord | None) -> str:
        if job.kind is JobKind.REINDEX:
            expected_prefix = f"sources/{job.school_id}/{job.document_id}/{job.version}/"
            if not job.source_key.startswith(expected_prefix):
                raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "source_key_mismatch")
            return job.source_key

        staged_prefix = f"{STAGED_PREFIX}{job.school_id}/{job.document_id}/{job.version}/"
        if not job.staged_key.startswith(staged_prefix):
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "staged_key_mismatch")
        if existing and existing.source_key:
            return existing.source_key  # a previous attempt already took ownership
        destination = source_key(job.school_id, job.document_id, job.version, job.extension)
        try:
            self.sources.copy_from_staging(job.staged_key, destination)
        except SourceUnavailableError:
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "source_unavailable") from None
        self.registry.set_version_fields(
            job.school_id, job.document_id, job.version, {"source_key": destination}
        )
        return destination

    def _chunks(self, job: IndexJob, key: str) -> list[Chunk]:
        try:
            downloaded = self.sources.download(key)
        except SourceUnavailableError:
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "source_unavailable") from None
        except SourceTooLargeError:
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "source_too_large") from None
        expected_version = f"v-{downloaded.sha256[:12]}"
        if job.version != expected_version or (job.sha256 and job.sha256 != downloaded.sha256):
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "hash_mismatch")
        try:
            document = extract(downloaded.data, job.extension)
            return chunk_document(
                document, title=job.title, max_input_tokens=self.embedder.max_input_tokens
            )
        except ExtractionError as exc:
            raise PermanentFailure(FailureReason(str(exc.reason)), exc.detail) from None
        except ChunkingError:
            raise PermanentFailure(FailureReason.EXTRACTION_FAILED, "chunking_failed") from None

    def _embed_and_write(self, job: IndexJob, chunks: list[Chunk], written: list[str]) -> int:
        tokens = 0
        size = self.embedder.max_batch_size
        for start in range(0, len(chunks), size):
            batch = chunks[start : start + size]
            documents = [c.embedding_document for c in batch]
            tokens += sum(estimate_tokens(d.prefixed_text) for d in documents)
            vectors = self.embedder.embed_documents(documents)  # step 6
            try:
                records = [
                    VectorRecord(
                        key=vector_key(job.document_id, job.version, chunk.chunk_no),
                        vector=vector,
                        metadata=build_metadata(
                            chunk,
                            school_id=job.school_id,
                            document_id=job.document_id,
                            version=job.version,
                            audience=job.audience,
                            title=job.title,
                        ),
                    )
                    for chunk, vector in zip(batch, vectors, strict=True)
                ]
            except MetadataTooLargeError:
                raise PermanentFailure(
                    FailureReason.EXTRACTION_FAILED, "metadata_too_large"
                ) from None
            self.store.put(records)  # step 7
            written.extend(r.key for r in records)
        return tokens

    def _finish_index(
        self,
        job: IndexJob,
        previous: str | None,
        list_key: str,
        chunk_count: int,
        tokens: int,
        started: float,
    ) -> IndexResult:
        fields = {
            "chunk_count": chunk_count,
            "vector_keys_key": list_key,
            "embedding_model": self.settings.embedding_model_id,
            "index_name": self.settings.index_name,
            "indexed_at": self.now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        activated, replaced = self._activate(job, previous, fields)  # step 9
        if not activated:
            # A newer upload is already live: this version never goes live.
            self._discard_version(job.school_id, job.document_id, job.version)
            self.registry.set_version_status(
                job.school_id, job.document_id, job.version, Status.SUPERSEDED, self.now()
            )
            self.logger.info("superseded by a newer upload before activation", active=replaced)
            return IndexResult(Outcome.SUPERSEDED, chunk_count, "newer_version_active")

        if replaced and replaced != job.version:  # step 10: write-then-delete
            self._discard_version(job.school_id, job.document_id, replaced)
            self.registry.set_version_status(
                job.school_id, job.document_id, replaced, Status.SUPERSEDED, self.now()
            )

        self.publisher.publish(  # step 11
            DetailType.DOCUMENT_INDEXED,
            school_id=job.school_id,
            correlation_id=job.import_id,
            data={
                "documentId": job.document_id,
                "version": job.version,
                "replacedVersion": replaced if replaced != job.version else None,
                "importId": job.import_id,
                "title": job.title,
                "audience": list(job.audience),
                "chunkCount": chunk_count,
                "embeddingModel": self.settings.embedding_model_id,
                "embeddingDimension": self.settings.embedding_dimension,
                "indexName": self.settings.index_name,
            },
        )
        self._record_success(chunk_count, tokens, started)
        self.logger.info("document indexed", chunk_count=chunk_count, replaced=replaced)
        return IndexResult(Outcome.INDEXED, chunk_count, replaced_version=replaced)

    def _finish_reindex(
        self, job: IndexJob, list_key: str, keys: list[str], tokens: int, started: float
    ) -> IndexResult:
        # The version is already live: no swap, no event (the uploader isn't told again).
        # The previous list named vectors in the old index, which goes away with it.
        self.sources.put_vector_keys(list_key, keys)
        self.registry.set_version_fields(
            job.school_id,
            job.document_id,
            job.version,
            {
                "index_name": self.settings.index_name,
                "embedding_model": self.settings.embedding_model_id,
                "chunk_count": len(keys),
                "vector_keys_key": list_key,
            },
        )
        self.registry.set_document_fields(
            job.school_id,
            job.document_id,
            {
                "embedding_model": self.settings.embedding_model_id,
                "index_name": self.settings.index_name,
            },
        )
        self._record_success(len(keys), tokens, started)
        self.logger.info("document reindexed", chunk_count=len(keys))
        return IndexResult(Outcome.REINDEXED, len(keys))

    def _activate(
        self, job: IndexJob, previous: str | None, fields: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Returns (activated, version that was active before)."""

        def attempt(expected: str | None) -> None:
            self.registry.activate(
                school_id=job.school_id,
                document_id=job.document_id,
                version=job.version,
                expected_active=expected,
                title=job.title,
                audience=job.audience,
                version_fields=fields,
                now=self.now(),
            )

        current = previous
        for _ in range(2):
            if current == job.version:
                return True, None
            current_record = (
                self.registry.get_version(job.school_id, job.document_id, current)
                if current
                else None
            )
            # Compare on every attempt, not only after a conflict: if a newer upload
            # finished before this one even started, there is no conflict to notice.
            if current is not None and not self._accepted_later(job.accepted_at, current_record):
                return False, current
            try:
                attempt(current)
                return True, current
            except ActivationConflictError:
                document = self.registry.get_document(job.school_id, job.document_id)
                current = document.active_version if document else None
        raise ActivationConflictError(job.document_id)  # still racing: let SQS retry

    @staticmethod
    def _accepted_later(accepted_at: str, other: VersionRecord | None) -> bool:
        if other is None or not other.accepted_at:
            return True
        if not accepted_at:
            return False
        return parse_time(accepted_at) > parse_time(other.accepted_at)

    def _discard_version(self, school_id: str, document_id: str, version: str) -> None:
        """Delete a version's vectors (via its key list), the list and the source copy."""
        VersionCleaner(self.registry, self.sources, self.store, self.settings.index_name).discard(
            school_id, document_id, version
        )

    # --- failures -----------------------------------------------------------------------

    def _fail_permanently(
        self,
        job: IndexJob,
        failure: PermanentFailure,
        planned_keys: list[str],
        written_keys: list[str],
    ) -> IndexResult:
        """Not retryable: clean up and report success to SQS."""
        self.logger.warning(
            "indexing failed permanently",
            reason_code=str(failure.reason),
            reason=failure.log_reason,
        )
        self._best_effort(lambda: self.store.delete(planned_keys or written_keys))
        if job.kind is JobKind.REINDEX:
            return IndexResult(
                Outcome.FAILED, reason=str(failure.reason)
            )  # active version untouched
        record = self.registry.get_version(job.school_id, job.document_id, job.version)
        if record:
            self._best_effort(lambda: self.sources.delete(record.vector_keys_key))
            self._best_effort(lambda: self.sources.delete(record.source_key))
        self.registry.set_version_status(
            job.school_id, job.document_id, job.version, Status.FAILED, self.now()
        )
        self.registry.mark_document_failed_if_never_active(
            job.school_id, job.document_id, self.now()
        )
        self._publish_failure(job, failure.reason)
        return IndexResult(Outcome.FAILED, reason=str(failure.reason))

    def _fail_retryably(
        self, job: IndexJob, exc: Exception, written_keys: list[str], receive_count: int
    ) -> None:
        """Remove partial vectors but keep the source copy for the retry. Tell people only
        on the last attempt, so a transient blip doesn't email the uploader."""
        reason = (
            FailureReason.EMBEDDING_PROVIDER_ERROR
            if isinstance(exc, ProviderError)
            else FailureReason.VECTOR_WRITE_FAILED
            if isinstance(exc, VectorStoreError)
            else None
        )
        self.logger.warning(
            "indexing attempt failed; will retry",
            error=type(exc).__name__,
            reason_code=str(reason) if reason else None,
            receive_count=receive_count,
        )
        self._best_effort(lambda: self.store.delete(written_keys))
        if job.kind is JobKind.REINDEX:
            return
        self._best_effort(
            lambda: self.registry.set_version_status(
                job.school_id, job.document_id, job.version, Status.FAILED, self.now()
            )
        )
        if receive_count >= self.settings.max_receive_count and reason is not None:
            self._best_effort(
                lambda: self.registry.mark_document_failed_if_never_active(
                    job.school_id, job.document_id, self.now()
                )
            )
            self._best_effort(lambda: self._publish_failure(job, reason))

    def _publish_failure(self, job: IndexJob, reason: FailureReason) -> None:
        self.publisher.publish(
            DetailType.DOCUMENT_INDEXING_FAILED,
            school_id=job.school_id,
            correlation_id=job.import_id,
            data={
                "documentId": job.document_id,
                "version": job.version,
                "importId": job.import_id,
                "reasonCode": str(reason),
            },
        )

    def _best_effort(self, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:
            self.logger.warning("cleanup step failed", error=type(exc).__name__)

    def _record_success(self, chunks: int, tokens: int, started: float) -> None:
        self.metrics.record("DocumentsIndexed", 1, "Count", {})
        self.metrics.record("ChunksIndexed", chunks, "Count", {})
        self.metrics.record("EmbeddingTokensEstimated", tokens, "Count", {})
        self.metrics.record(
            "IndexingDuration", (time.monotonic() - started) * 1000, "Milliseconds", {}
        )

from __future__ import annotations

import json
from typing import Any

import pytest

from knowledge.indexer import Outcome
from knowledge.jobs import IndexJob
from knowledge.providers.base import EmbeddingDocument
from knowledge.providers.errors import ProviderRetryableError
from knowledge.providers.fakes import FakeEmbedder
from knowledge.registry import ActivationConflictError, Status
from knowledge.vector_store import InMemoryVectorStore, VectorStoreError
from tests.unit.knowledge.documents import scanned_pdf
from tests.unit.knowledge.indexing_harness import (
    INGESTION_BUCKET,
    KNOWLEDGE_BUCKET,
    SCHOOL,
    KnowledgeHarness,
    policy_markdown,
)

V1 = policy_markdown("Code of Conduct 2027", marker="first")
V2 = policy_markdown("Code of Conduct 2027", marker="second")


def _source_key(job: IndexJob) -> str:
    return f"sources/{SCHOOL}/{job.document_id}/{job.version}/source.{job.extension}"


def _list_key(job: IndexJob) -> str:
    return f"vector-keys/{SCHOOL}/{job.document_id}/{job.version}.json"


class FailingEmbedder(FakeEmbedder):
    """Fails on the Nth embed_documents call."""

    def __init__(self, fail_on: int, error: Exception | None = None) -> None:
        super().__init__(max_batch_size=4)
        self.fail_on = fail_on
        self.error = error or ProviderRetryableError("gemini", "server error", status_code=503)

    def embed_documents(self, documents: Any) -> list[list[float]]:
        if len(self.calls) + 1 == self.fail_on:
            self.calls.append(len(documents))
            raise self.error
        return super().embed_documents(documents)


# --- first upload ----------------------------------------------------------------------


def test_first_upload_end_to_end(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    result = kh.indexer().process(job)
    assert result.outcome is Outcome.INDEXED
    assert result.chunk_count > 1

    # The source now belongs to this service (a byte-identical copy).
    copy = kh.s3.get_object(Bucket=KNOWLEDGE_BUCKET, Key=_source_key(job))["Body"].read()
    assert copy == V1

    version = kh.registry.get_version(SCHOOL, job.document_id, job.version)
    assert version is not None
    assert version.status == Status.ACTIVE
    assert version.source_key == _source_key(job)
    assert version.vector_keys_key == _list_key(job)
    assert version.chunk_count == result.chunk_count
    assert version.embedding_model == "fake/hashed-bag-of-words"
    assert version.index_name == "docs-gemini-768-v1"
    document = kh.registry.get_document(SCHOOL, job.document_id)
    assert document is not None
    assert document.active_version == job.version
    assert document.status == Status.ACTIVE

    # Vectors, their key list, and their metadata.
    keys = kh.store.keys_for(job.document_id, job.version)
    assert len(keys) == result.chunk_count
    assert sorted(kh.read_json(_list_key(job))) == sorted(keys)
    record = kh.store.vectors[f"{job.document_id}#{job.version}#00000"]
    assert record.metadata["school_id"] == SCHOOL
    assert record.metadata["audience"] == ["STAFF", "PARENT"]
    assert record.metadata["title"] == "Code of Conduct 2027"
    assert record.metadata["heading_path"].startswith("Code of Conduct 2027")
    assert len(record.vector) == kh.embedder.dimension

    (event,) = kh.events_of("DocumentIndexed")
    assert event["data"] == {
        "documentId": job.document_id,
        "version": job.version,
        "replacedVersion": None,
        "importId": job.import_id,
        "title": "Code of Conduct 2027",
        "audience": ["STAFF", "PARENT"],
        "chunkCount": result.chunk_count,
        "embeddingModel": "fake/hashed-bag-of-words",
        "embeddingDimension": kh.embedder.dimension,
        "indexName": "docs-gemini-768-v1",
    }
    assert event["metadata"]["correlationId"] == job.import_id
    assert event["metadata"]["producer"] == "knowledge-service"
    assert kh.events.entries[0]["Source"] == "school.knowledge"
    assert kh.metrics.value("DocumentsIndexed") == 1
    assert kh.metrics.value("ChunksIndexed") == result.chunk_count


def test_embeddings_are_batched_by_the_embedders_limit(kh: KnowledgeHarness) -> None:
    embedder = FakeEmbedder(max_batch_size=3)
    result = kh.indexer(embedder=embedder).process(kh.stage(V1))
    assert all(size <= 3 for size in embedder.calls)
    assert sum(embedder.calls) == result.chunk_count


def test_logs_never_contain_document_text(kh: KnowledgeHarness) -> None:
    kh.indexer().process(kh.stage(V1))
    assert "learners and parents follow school policy" not in kh.logs()
    assert "document indexed" in kh.logs()


# --- versions ----------------------------------------------------------------------------


def test_reupload_swaps_versions_and_removes_the_old_one(kh: KnowledgeHarness) -> None:
    first = kh.stage(V1, accepted_at="2026-11-12T08:00:00.000Z")
    second = kh.stage(V2, accepted_at="2026-11-12T09:00:00.000Z")
    kh.indexer().process(first)
    result = kh.indexer().process(second)

    assert result.outcome is Outcome.INDEXED
    assert result.replaced_version == first.version
    document = kh.registry.get_document(SCHOOL, first.document_id)
    assert document is not None
    assert document.active_version == second.version
    old = kh.registry.get_version(SCHOOL, first.document_id, first.version)
    assert old is not None
    assert old.status == Status.SUPERSEDED
    assert kh.store.keys_for(first.document_id, first.version) == set()  # old vectors gone
    assert kh.store.keys_for(second.document_id, second.version)
    assert _source_key(first) not in kh.knowledge_keys()  # old source copy gone
    assert _list_key(first) not in kh.knowledge_keys()  # old key list gone
    assert kh.events_of("DocumentIndexed")[-1]["data"]["replacedVersion"] == first.version


def test_newer_upload_that_finished_first_stays_active(kh: KnowledgeHarness) -> None:
    older = kh.stage(V1, accepted_at="2026-11-12T08:00:00.000Z")
    newer = kh.stage(V2, accepted_at="2026-11-12T09:00:00.000Z")
    kh.indexer().process(newer)  # finishes first
    result = kh.indexer().process(older)  # finishes last, but is the older upload

    assert result.outcome is Outcome.SUPERSEDED
    document = kh.registry.get_document(SCHOOL, older.document_id)
    assert document is not None
    assert document.active_version == newer.version
    assert kh.store.keys_for(older.document_id, older.version) == set()
    assert kh.store.keys_for(newer.document_id, newer.version)
    assert _source_key(older) not in kh.knowledge_keys()
    assert len(kh.events_of("DocumentIndexed")) == 1  # nobody is told the old one went live


def test_activation_race_is_resolved_by_upload_time(
    kh: KnowledgeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another version becomes active between our read and our swap."""
    older = kh.stage(V1, accepted_at="2026-11-12T08:00:00.000Z")
    newer = kh.stage(V2, accepted_at="2026-11-12T09:00:00.000Z")
    indexer = kh.indexer()
    registry = indexer.registry
    real_activate = registry.activate
    calls: list[str | None] = []

    def racing_activate(**kwargs: Any) -> None:
        calls.append(kwargs["expected_active"])
        if len(calls) == 1:
            kh.indexer().process(older)  # the older upload slips in first
            raise ActivationConflictError(kwargs["document_id"])
        real_activate(**kwargs)

    monkeypatch.setattr(registry, "activate", racing_activate)
    result = indexer.process(newer)
    assert result.outcome is Outcome.INDEXED
    assert calls == [None, older.version]  # retried against the version that won the race
    document = kh.registry.get_document(SCHOOL, newer.document_id)
    assert document is not None
    assert document.active_version == newer.version
    assert kh.store.keys_for(older.document_id, older.version) == set()


def test_same_version_already_active_is_skipped(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    kh.indexer().process(job)
    puts = len(kh.store.put_calls)
    result = kh.indexer().process(job)
    assert result.outcome is Outcome.SKIPPED
    assert len(kh.store.put_calls) == puts
    assert len(kh.events_of("DocumentIndexed")) == 1


# --- source handling ---------------------------------------------------------------------


def test_hash_mismatch_is_a_permanent_extraction_failure(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    tampered = IndexJob(**{**job.__dict__, "sha256": "0" * 64})
    result = kh.indexer().process(tampered)
    assert (result.outcome, result.reason) == (Outcome.FAILED, "EXTRACTION_FAILED")
    assert "hash_mismatch" in kh.logs()
    (event,) = kh.events_of("DocumentIndexingFailed")
    assert event["data"]["reasonCode"] == "EXTRACTION_FAILED"
    assert kh.knowledge_keys() == []  # permanent failure: the copy is deleted


def test_version_must_match_the_content_hash(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    wrong = IndexJob(
        **{
            **job.__dict__,
            "version": "v-000000000000",
            "staged_key": job.staged_key.replace(job.version, "v-000000000000"),
        }
    )
    kh.s3.copy_object(
        Bucket=INGESTION_BUCKET,
        Key=wrong.staged_key,
        CopySource={"Bucket": INGESTION_BUCKET, "Key": job.staged_key},
    )
    assert kh.indexer().process(wrong).reason == "EXTRACTION_FAILED"


def test_missing_staged_copy_with_our_own_copy_succeeds(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    # A first attempt took ownership of the source, then failed (e.g. provider outage).
    with pytest.raises(ProviderRetryableError):
        kh.indexer(embedder=FailingEmbedder(fail_on=1)).process(job)
    assert _source_key(job) in kh.knowledge_keys()  # kept for the retry
    kh.s3.delete_object(Bucket=INGESTION_BUCKET, Key=job.staged_key)  # staging expired
    assert kh.indexer().process(job, receive_count=2).outcome is Outcome.INDEXED


def test_missing_staged_copy_without_our_copy_fails(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1, upload=False)
    result = kh.indexer().process(job)
    assert (result.outcome, result.reason) == (Outcome.FAILED, "EXTRACTION_FAILED")
    assert "source_unavailable" in kh.logs()


def test_staged_key_must_belong_to_this_document(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    other_school = IndexJob(
        **{**job.__dict__, "staged_key": job.staged_key.replace(SCHOOL, "SCH-002")}
    )
    result = kh.indexer().process(other_school)
    assert result.reason == "EXTRACTION_FAILED"
    assert "staged_key_mismatch" in kh.logs()


# --- failures -----------------------------------------------------------------------------


def test_scanned_pdf_fails_permanently_and_cleans_up(kh: KnowledgeHarness) -> None:
    job = kh.stage(scanned_pdf(), extension="pdf")
    result = kh.indexer().process(job)
    assert (result.outcome, result.reason) == (Outcome.FAILED, "NO_EXTRACTABLE_TEXT")
    version = kh.registry.get_version(SCHOOL, job.document_id, job.version)
    assert version is not None
    assert version.status == Status.FAILED
    document = kh.registry.get_document(SCHOOL, job.document_id)
    assert document is not None
    assert document.status == Status.FAILED  # never had an active version
    assert kh.knowledge_keys() == []
    (event,) = kh.events_of("DocumentIndexingFailed")
    assert event["data"] == {
        "documentId": job.document_id,
        "version": job.version,
        "importId": job.import_id,
        "reasonCode": "NO_EXTRACTABLE_TEXT",
    }


def test_failed_reupload_keeps_the_live_version(kh: KnowledgeHarness) -> None:
    good = kh.stage(V1, accepted_at="2026-11-12T08:00:00.000Z")
    kh.indexer().process(good)
    bad = kh.stage(scanned_pdf(), extension="pdf", accepted_at="2026-11-12T09:00:00.000Z")
    assert kh.indexer().process(bad).outcome is Outcome.FAILED
    document = kh.registry.get_document(SCHOOL, good.document_id)
    assert document is not None
    assert (document.status, document.active_version) == (Status.ACTIVE, good.version)
    assert kh.store.keys_for(good.document_id, good.version)
    assert _source_key(good) in kh.knowledge_keys()


def test_retryable_failure_removes_partial_vectors_and_keeps_the_source(
    kh: KnowledgeHarness,
) -> None:
    job = kh.stage(V1)
    embedder = FailingEmbedder(fail_on=2)  # first batch written, second fails
    with pytest.raises(ProviderRetryableError):
        kh.indexer(embedder=embedder).process(job, receive_count=1)
    assert kh.store.put_calls  # something was written...
    assert kh.store.keys_for(job.document_id, job.version) == set()  # ...and removed
    assert _source_key(job) in kh.knowledge_keys()
    version = kh.registry.get_version(SCHOOL, job.document_id, job.version)
    assert version is not None
    assert version.status == Status.FAILED
    assert kh.events_of("DocumentIndexingFailed") == []  # not the last attempt: stay quiet


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda kh: kh.indexer(embedder=FailingEmbedder(fail_on=1)), "EMBEDDING_PROVIDER_ERROR"),
        (lambda kh: kh.indexer(store=InMemoryVectorStore(fail_on_put=1)), "VECTOR_WRITE_FAILED"),
    ],
)
def test_last_attempt_reports_the_failure(kh: KnowledgeHarness, make: Any, reason: str) -> None:
    job = kh.stage(V1)
    with pytest.raises((ProviderRetryableError, VectorStoreError)):
        make(kh).process(job, receive_count=3)
    (event,) = kh.events_of("DocumentIndexingFailed")
    assert event["data"]["reasonCode"] == reason


def test_key_list_exists_before_any_vector_is_written(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    seen: list[list[str]] = []

    class CheckingStore(InMemoryVectorStore):
        def put(self, records: Any) -> None:
            seen.append(kh.knowledge_keys("vector-keys/"))
            super().put(records)

    kh.indexer(store=CheckingStore()).process(job)
    assert seen
    assert all(_list_key(job) in keys for keys in seen)


# --- re-index ----------------------------------------------------------------------------


def test_reindex_writes_to_the_configured_index_and_publishes_nothing(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    kh.indexer().process(job)
    kh.s3.delete_object(Bucket=INGESTION_BUCKET, Key=job.staged_key)  # staging long gone
    new_store = InMemoryVectorStore()
    new_model = FakeEmbedder(dimension=32, model_id="fake/new-model")
    events_before = len(kh.events.entries)

    result = kh.indexer(
        index_name="docs-gemini-768-v2", store=new_store, embedder=new_model
    ).process(kh.reindex_job(job))
    assert result.outcome is Outcome.REINDEXED
    assert len(new_store.keys_for(job.document_id, job.version)) == result.chunk_count
    version = kh.registry.get_version(SCHOOL, job.document_id, job.version)
    assert version is not None
    assert version.index_name == "docs-gemini-768-v2"
    assert version.embedding_model == "fake/new-model"
    assert version.status == Status.ACTIVE
    assert len(kh.events.entries) == events_before  # nothing published


def test_reindex_is_idempotent_per_index(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    kh.indexer().process(job)
    reindex = kh.reindex_job(job)
    assert kh.indexer().process(reindex).outcome is Outcome.SKIPPED  # already in this index
    second_index = kh.indexer(index_name="docs-gemini-768-v2", store=InMemoryVectorStore())
    assert second_index.process(reindex).outcome is Outcome.REINDEXED


def test_reindex_of_an_inactive_version_is_skipped(kh: KnowledgeHarness) -> None:
    first = kh.stage(V1, accepted_at="2026-11-12T08:00:00.000Z")
    second = kh.stage(V2, accepted_at="2026-11-12T09:00:00.000Z")
    kh.indexer().process(first)
    kh.indexer().process(second)
    result = kh.indexer(index_name="docs-gemini-768-v2").process(kh.reindex_job(first))
    assert (result.outcome, result.reason) == (Outcome.SKIPPED, "not_active")


def test_failed_reindex_never_touches_the_live_version(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    kh.indexer().process(job)
    new_store = InMemoryVectorStore()
    with pytest.raises(ProviderRetryableError):
        kh.indexer(
            index_name="docs-gemini-768-v2", store=new_store, embedder=FailingEmbedder(fail_on=2)
        ).process(kh.reindex_job(job), receive_count=3)
    version = kh.registry.get_version(SCHOOL, job.document_id, job.version)
    assert version is not None
    assert (version.status, version.index_name) == (Status.ACTIVE, "docs-gemini-768-v1")
    assert _source_key(job) in kh.knowledge_keys()
    assert new_store.keys_for(job.document_id) == set()  # partial vectors removed
    assert kh.events_of("DocumentIndexingFailed") == []


def test_reindex_source_must_belong_to_the_version(kh: KnowledgeHarness) -> None:
    job = kh.stage(V1)
    kh.indexer().process(job)
    reindex = kh.reindex_job(job)
    bad = IndexJob(**{**reindex.__dict__, "source_key": "sources/SCH-002/other/source.md"})
    assert kh.indexer(index_name="docs-gemini-768-v2").process(bad).reason == "EXTRACTION_FAILED"


def test_embedding_document_has_context_prefix(kh: KnowledgeHarness) -> None:
    seen: list[EmbeddingDocument] = []

    class Spy(FakeEmbedder):
        def embed_documents(self, documents: Any) -> list[list[float]]:
            seen.extend(documents)
            return super().embed_documents(documents)

    kh.indexer(embedder=Spy()).process(kh.stage(V1))
    assert seen[0].context == "Code of Conduct 2027 > Section 1"  # title not repeated
    assert json.dumps(seen[0].text)  # plain text, no prefix in the stored text

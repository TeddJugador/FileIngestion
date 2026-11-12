from __future__ import annotations

import json

import pytest

from knowledge.identity import Caller, Role
from knowledge.providers.base import GenerationRequest, GenerationResult
from knowledge.providers.errors import ProviderRetryableError
from knowledge.providers.fakes import FakeEmbedder, FakeGenerator
from knowledge.query import (
    NO_SOURCES_MESSAGE,
    DocumentNotFoundError,
    FeedbackNotAllowedError,
    ModelMismatchError,
    Outcome,
)
from knowledge.registry import Status
from knowledge.vector_store import VectorRecord
from tests.unit.knowledge.indexing_harness import KnowledgeHarness
from tests.unit.knowledge.query_harness import (
    ADMIN,
    PARENT,
    STAFF,
    create_query_log,
    index_text,
    query_service,
)

FEES = (
    "# Fee Policy\n\n## Due dates\n\n"
    "School fees are due on the seventh day of every month. Late fees apply after the seventh."
)
STAFF_ONLY = (
    "# Staff Disciplinary Procedure\n\n## Hearings\n\n"
    "School fees disputes with staff are heard by the governing body on the seventh day."
)
UNIFORM = "# Uniform Policy\n\n## Shoes\n\nLearners wear black leather school shoes every day."


@pytest.fixture
def indexed(kh: KnowledgeHarness) -> KnowledgeHarness:
    create_query_log()
    index_text(
        kh, FEES, document_id="doc-aaaaaaaaaaa1", audience=("PARENT", "STAFF"), title="Fee Policy"
    )
    index_text(
        kh, STAFF_ONLY, document_id="doc-aaaaaaaaaaa2", audience=("STAFF",), title="Staff Procedure"
    )
    index_text(
        kh, UNIFORM, document_id="doc-aaaaaaaaaaa3", audience=("PARENT",), title="Uniform Policy"
    )
    return kh


QUESTION = "When are school fees due each month?"


def _cited(result: object) -> set[str]:
    return {c.document_id for c in result.citations}  # type: ignore[attr-defined]


# --- authorization: audience and school -----------------------------------------------


def test_parent_never_sees_staff_only_content(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator()
    result = query_service(indexed, generator=generator).ask(PARENT, QUESTION)
    assert result.outcome is Outcome.ANSWERED
    assert "doc-aaaaaaaaaaa2" not in _cited(result)
    prompt = generator.requests[0].user
    assert "governing body" not in prompt  # staff-only text never reached the model
    # The filter was applied by the vector store itself, not just afterwards.
    assert indexed.store.queries[-1] == {
        "$and": [
            {"school_id": {"$eq": "SCH-001"}},
            {"$or": [{"audience": {"$eq": "PARENT"}}]},
        ]
    }


def test_staff_see_staff_and_parent_content(indexed: KnowledgeHarness) -> None:
    result = query_service(indexed).ask(STAFF, QUESTION)
    assert {"doc-aaaaaaaaaaa1", "doc-aaaaaaaaaaa2"} <= _cited(result)


def test_schools_are_isolated(kh: KnowledgeHarness) -> None:
    create_query_log()
    index_text(
        kh,
        FEES,
        document_id="doc-bbbbbbbbbbb1",
        audience=("PARENT",),
        title="Fees",
        school_id="SCH-002",
    )
    other_school = Caller(user_id="u", school_id="SCH-001", role=Role.ADMIN)
    result = query_service(kh).ask(other_school, QUESTION)
    assert result.outcome is Outcome.NO_RELEVANT_SOURCES


# --- ranking and filtering ---------------------------------------------------------------


def test_inactive_versions_are_dropped(indexed: KnowledgeHarness) -> None:
    """During a swap, the old version's vectors may still be in the index briefly."""
    stale = next(r for k, r in indexed.store.vectors.items() if k.startswith("doc-aaaaaaaaaaa1"))
    indexed.store.vectors["doc-aaaaaaaaaaa1#v-000000000000#00000"] = VectorRecord(
        key="doc-aaaaaaaaaaa1#v-000000000000#00000",
        vector=stale.vector,
        metadata={**stale.metadata, "version": "v-000000000000", "chunk_text": "OLD fees text"},
    )
    generator = FakeGenerator()
    query_service(indexed, generator=generator).ask(PARENT, QUESTION)
    assert "OLD fees text" not in generator.requests[0].user


def test_threshold_and_no_sources_skip_the_model(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator()
    result = query_service(indexed, generator=generator, threshold=0.99).ask(PARENT, QUESTION)
    assert result.outcome is Outcome.NO_RELEVANT_SOURCES
    assert result.answer == NO_SOURCES_MESSAGE
    assert result.grounded is False
    assert generator.requests == []  # no model call
    assert indexed.metrics.value("NoRelevantSourcesCount") == 1
    (event,) = indexed.events_of("QuestionAnswered")
    assert event["data"]["outcome"] == "NO_RELEVANT_SOURCES"


def test_at_most_two_chunks_per_section(kh: KnowledgeHarness) -> None:
    create_query_log()
    long_section = "# Fees\n\n## Due dates\n\n" + " ".join(
        f"School fees rule {i} says fees are due monthly and late fees apply." for i in range(400)
    )
    index_text(kh, long_section, document_id="doc-ccccccccccc1", audience=("PARENT",), title="Fees")
    generator = FakeGenerator()
    query_service(kh, generator=generator).ask(PARENT, QUESTION, top_k=10)
    assert generator.requests[0].user.count('<source id="') == 2


def test_top_k_limits_sources(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator()
    query_service(indexed, generator=generator).ask(STAFF, QUESTION, top_k=1)
    assert generator.requests[0].user.count('<source id="') == 1


# --- citations ------------------------------------------------------------------------


def test_invented_citations_are_dropped(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator(answer="Fees are due on the 7th [S1][S9]. See also [S7].")
    result = query_service(indexed, generator=generator).ask(PARENT, QUESTION)
    assert result.answer == "Fees are due on the 7th [S1]. See also."
    assert [c.source_id for c in result.citations] == ["S1"]
    assert result.grounded is True


def test_answer_without_valid_citations_is_ungrounded(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator(answer="Fees are probably due at some point [S42].")
    result = query_service(indexed, generator=generator).ask(PARENT, QUESTION)
    assert result.grounded is False
    assert result.citations == []
    assert indexed.metrics.value("UngroundedAnswerCount") == 1


def test_citations_map_to_documents_and_sections(indexed: KnowledgeHarness) -> None:
    result = query_service(indexed).ask(PARENT, QUESTION)
    fee = next(c for c in result.citations if c.document_id == "doc-aaaaaaaaaaa1")
    assert fee.title == "Fee Policy"
    assert fee.heading_path == "Fee Policy > Due dates"
    assert fee.page_start is None  # Markdown has no pages


# --- failures and events ----------------------------------------------------------------


class _FailingGenerator(FakeGenerator):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise ProviderRetryableError("claude", "overloaded", status_code=529)


def test_generation_failure_is_a_provider_error(indexed: KnowledgeHarness) -> None:
    result = query_service(indexed, generator=_FailingGenerator()).ask(PARENT, QUESTION)
    assert result.outcome is Outcome.PROVIDER_ERROR
    (event,) = indexed.events_of("QuestionAnswered")
    assert event["data"]["outcome"] == "PROVIDER_ERROR"


def test_embedding_failure_is_a_provider_error(indexed: KnowledgeHarness) -> None:
    class Failing(FakeEmbedder):
        def embed_query(self, text: str) -> list[float]:
            raise ProviderRetryableError("gemini", "throttled", status_code=429)

    service = query_service(indexed)
    service.embedder = Failing()
    assert service.ask(PARENT, QUESTION).outcome is Outcome.PROVIDER_ERROR


def test_model_mismatch_refuses_and_alarms(indexed: KnowledgeHarness) -> None:
    service = query_service(indexed, embedding_model_id="voyage/voyage-4")
    with pytest.raises(ModelMismatchError):
        service.ask(PARENT, QUESTION)
    assert indexed.metrics.value("EmbeddingModelMismatch") == 1


def test_question_answered_event_has_no_question_or_answer(indexed: KnowledgeHarness) -> None:
    result = query_service(indexed).ask(PARENT, "My son Thabo is late often. When are fees due?")
    (event,) = indexed.events_of("QuestionAnswered")
    data = event["data"]
    assert set(data) == {
        "queryId",
        "askerId",
        "askerRole",
        "outcome",
        "chunksRetrieved",
        "citedDocumentIds",
        "topScore",
        "latencyMs",
        "embeddingModel",
        "generationProvider",
        "generationModel",
        "inputTokens",
        "outputTokens",
    }
    assert data["queryId"] == result.query_id
    assert data["askerId"] == PARENT.user_id
    assert data["askerRole"] == "PARENT"
    assert "Thabo" not in json.dumps(event)
    assert "Thabo" not in indexed.logs()
    assert event["metadata"]["correlationId"] == result.query_id


# --- feedback ---------------------------------------------------------------------------


def test_feedback_on_own_query(indexed: KnowledgeHarness) -> None:
    service = query_service(indexed)
    result = service.ask(PARENT, QUESTION)
    service.feedback(PARENT, result.query_id, helpful=False, reason_code="INCOMPLETE")
    (event,) = indexed.events_of("AnswerFeedbackGiven")
    assert event["data"] == {
        "queryId": result.query_id,
        "helpful": False,
        "reasonCode": "INCOMPLETE",
    }


def test_feedback_on_someone_elses_query_is_refused(indexed: KnowledgeHarness) -> None:
    service = query_service(indexed)
    result = service.ask(PARENT, QUESTION)
    other = Caller(user_id="another-parent", school_id="SCH-001", role=Role.PARENT)
    with pytest.raises(FeedbackNotAllowedError):
        service.feedback(other, result.query_id, helpful=True, reason_code=None)
    with pytest.raises(FeedbackNotAllowedError):
        service.feedback(PARENT, "qry-01JD0000000000000000000000", helpful=True, reason_code=None)
    assert indexed.events_of("AnswerFeedbackGiven") == []


def test_query_log_stores_no_text(indexed: KnowledgeHarness) -> None:
    result = query_service(indexed).ask(PARENT, QUESTION)
    item = indexed.dynamodb.get_item(TableName="QueryLog", Key={"queryId": {"S": result.query_id}})[
        "Item"
    ]
    assert set(item) == {"queryId", "askerId", "expires_at"}


# --- admin: list and retire ---------------------------------------------------------------


def test_list_documents_for_the_callers_school(indexed: KnowledgeHarness) -> None:
    documents = query_service(indexed).list_documents(ADMIN)
    assert {d["documentId"] for d in documents} == {
        "doc-aaaaaaaaaaa1",
        "doc-aaaaaaaaaaa2",
        "doc-aaaaaaaaaaa3",
    }
    assert all(d["status"] == "ACTIVE" for d in documents)


def test_retire_removes_vectors_files_and_publishes(indexed: KnowledgeHarness) -> None:
    service = query_service(indexed)
    version = indexed.registry.get_document("SCH-001", "doc-aaaaaaaaaaa1").active_version  # type: ignore[union-attr]
    assert version
    result = service.retire(ADMIN, "doc-aaaaaaaaaaa1")
    assert result == {"documentId": "doc-aaaaaaaaaaa1", "version": version, "status": "RETIRED"}

    assert indexed.store.keys_for("doc-aaaaaaaaaaa1") == set()
    assert not [k for k in indexed.knowledge_keys() if "doc-aaaaaaaaaaa1" in k]  # source + key list
    document = indexed.registry.get_document("SCH-001", "doc-aaaaaaaaaaa1")
    assert document is not None
    assert (document.status, document.active_version) == (Status.RETIRED, None)
    (event,) = indexed.events_of("DocumentRetired")
    assert event["data"] == {
        "documentId": "doc-aaaaaaaaaaa1",
        "version": version,
        "retiredBy": ADMIN.user_id,
    }
    assert "doc-aaaaaaaaaaa1" not in _cited(service.ask(PARENT, QUESTION))


def test_retire_unknown_or_already_retired(indexed: KnowledgeHarness) -> None:
    service = query_service(indexed)
    with pytest.raises(DocumentNotFoundError):
        service.retire(ADMIN, "doc-ffffffffffff")
    service.retire(ADMIN, "doc-aaaaaaaaaaa3")
    with pytest.raises(DocumentNotFoundError):
        service.retire(ADMIN, "doc-aaaaaaaaaaa3")


def test_retrieve_returns_what_ask_would_send(indexed: KnowledgeHarness) -> None:
    generator = FakeGenerator()
    service = query_service(indexed, generator=generator)
    retrieved = service.retrieve(PARENT, QUESTION)
    service.ask(PARENT, QUESTION)
    prompt = generator.requests[0].user
    assert retrieved
    assert prompt.count('<source id="') == len(retrieved)
    for hit in retrieved:
        assert hit.metadata["chunk_text"].split(".")[0] in prompt
    assert all("PARENT" in hit.metadata["audience"] for hit in retrieved)

"""Answering questions (spec section 6), feedback, and the admin document routes.

Plain Python with injected dependencies; ``handlers/api.py`` adds HTTP. The question
and answer text are never logged, stored or put in events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from aws_lambda_powertools import Logger
from ulid import ULID

from knowledge.events import DetailType, Publisher
from knowledge.identity import Caller
from knowledge.indexer import MetricsSink
from knowledge.prompting import (
    MAX_TOKENS,
    SYSTEM_PROMPT,
    Citation,
    Source,
    build_user_prompt,
    resolve_citations,
)
from knowledge.providers.base import Embedder, GenerationRequest, Generator
from knowledge.providers.errors import ProviderError
from knowledge.query_log import QueryLog
from knowledge.registry import Registry, RetireConflictError, Status
from knowledge.vector_store import QueryHit, VectorStore, audience_filter
from knowledge.versions import VersionCleaner

DEFAULT_TOP_K = 6
MAX_PER_SECTION = 2
NO_SOURCES_MESSAGE = (
    "I couldn't find anything about this in the school's documents. "
    "Please contact the school office, who will be happy to help."
)
PROVIDER_ERROR_MESSAGE = "The assistant is temporarily unavailable. Please try again shortly."


class Outcome(StrEnum):
    ANSWERED = "ANSWERED"
    NO_RELEVANT_SOURCES = "NO_RELEVANT_SOURCES"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ModelMismatchError(Exception):
    """The configured embedder didn't build the index being queried: refuse to answer."""


class DocumentNotFoundError(Exception):
    pass


class FeedbackNotAllowedError(Exception):
    """Unknown query, or someone else's: reported as not found either way."""


@dataclass(frozen=True)
class QuerySettings:
    index_name: str
    embedding_model_id: str
    generation_provider: str
    generation_model: str
    similarity_threshold: float = 0.35


@dataclass(frozen=True)
class AskResult:
    query_id: str
    outcome: Outcome
    answer: str
    grounded: bool
    citations: list[Citation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queryId": self.query_id,
            "outcome": str(self.outcome),
            "answer": self.answer,
            "grounded": self.grounded,
            "citations": [c.to_dict() for c in self.citations],
        }


@dataclass
class QueryService:
    settings: QuerySettings
    registry: Registry
    store: VectorStore
    embedder: Embedder
    generator: Generator
    publisher: Publisher
    query_log: QueryLog
    cleaner: VersionCleaner
    metrics: MetricsSink
    logger: Logger
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_query_id: Callable[[], str] = lambda: f"qry-{ULID()}"

    # --- POST /ask ------------------------------------------------------------------

    def ask(self, caller: Caller, question: str, top_k: int = DEFAULT_TOP_K) -> AskResult:
        started = time.monotonic()
        query_id = self.new_query_id()
        self.logger.set_correlation_id(query_id)
        self.logger.append_keys(
            query_id=query_id, school_id=caller.school_id, role=str(caller.role)
        )
        self.logger.info("question received", question_chars=len(question), top_k=top_k)
        self.query_log.record(query_id, caller.user_id, self.now())

        try:
            vector = self.embedder.embed_query(question)
        except ProviderError as exc:
            return self._provider_error(caller, query_id, started, exc)

        retrieval_started = time.monotonic()
        hits = self.store.query(
            vector,
            top_k=top_k * 2,
            filter=audience_filter(caller.school_id, caller.audiences),  # authorization
        )
        self._metric(
            "RetrievalLatency", (time.monotonic() - retrieval_started) * 1000, "Milliseconds"
        )
        selected = self._select(caller, hits, top_k)
        top_score = max((h.similarity for h in selected), default=0.0)

        if not selected:  # no sources: no model call (cost, and no ungrounded answers)
            self._metric("NoRelevantSourcesCount", 1)
            self._finish(caller, query_id, Outcome.NO_RELEVANT_SOURCES, started, [], [], top_score)
            return AskResult(query_id, Outcome.NO_RELEVANT_SOURCES, NO_SOURCES_MESSAGE, False)

        sources = [_source(f"S{i}", hit) for i, hit in enumerate(selected, start=1)]
        generation_started = time.monotonic()
        try:
            result = self.generator.generate(
                GenerationRequest(
                    system=SYSTEM_PROMPT,
                    user=build_user_prompt(question, sources),
                    max_tokens=MAX_TOKENS,
                )
            )
        except ProviderError as exc:
            return self._provider_error(caller, query_id, started, exc)
        self._metric(
            "GenerationLatency", (time.monotonic() - generation_started) * 1000, "Milliseconds"
        )
        self._metric("GenerationInputTokens", result.input_tokens)
        self._metric("GenerationOutputTokens", result.output_tokens)

        answer, citations = resolve_citations(result.text, sources)
        grounded = bool(citations)
        if not grounded:
            self._metric("UngroundedAnswerCount", 1)
        self.logger.info(
            "question answered",
            sources=len(sources),
            citations=len(citations),
            grounded=grounded,
            truncated=result.truncated,
            top_score=round(top_score, 3),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        self._finish(
            caller,
            query_id,
            Outcome.ANSWERED,
            started,
            sources,
            citations,
            top_score,
            tokens=(result.input_tokens, result.output_tokens),
        )
        return AskResult(query_id, Outcome.ANSWERED, answer, grounded, citations)

    def _select(self, caller: Caller, hits: list[QueryHit], top_k: int) -> list[QueryHit]:
        documents = self.registry.get_documents(
            caller.school_id, {str(h.metadata.get("document_id", "")) for h in hits}
        )
        mismatched = [
            d
            for d in documents.values()
            if d.index_name == self.settings.index_name
            and d.embedding_model
            and d.embedding_model != self.settings.embedding_model_id
        ]
        if mismatched:
            self._metric("EmbeddingModelMismatch", 1)
            self.logger.error(
                "embedding model mismatch: refusing to answer",
                configured=self.settings.embedding_model_id,
                indexed=mismatched[0].embedding_model,
            )
            raise ModelMismatchError(self.settings.embedding_model_id)

        selected: list[QueryHit] = []
        per_section: dict[tuple[str, str], int] = {}
        for hit in sorted(hits, key=lambda h: h.similarity, reverse=True):
            meta = hit.metadata
            document = documents.get(str(meta.get("document_id", "")))
            if (
                document is None
                or document.status != Status.ACTIVE
                or meta.get("version") != document.active_version  # mid-swap overlap
                or meta.get("school_id") != caller.school_id  # belt and braces
                or not set(meta.get("audience", [])) & set(caller.audiences)
                or hit.similarity < self.settings.similarity_threshold
            ):
                continue
            section = (document.document_id, str(meta.get("heading_path", "")))
            if per_section.get(section, 0) >= MAX_PER_SECTION:
                continue
            per_section[section] = per_section.get(section, 0) + 1
            selected.append(hit)
            if len(selected) == top_k:
                break
        return selected

    def _provider_error(
        self, caller: Caller, query_id: str, started: float, exc: ProviderError
    ) -> AskResult:
        self.logger.warning(
            "provider error while answering", provider=exc.provider, status=exc.status_code
        )
        self._finish(caller, query_id, Outcome.PROVIDER_ERROR, started, [], [], 0.0)
        return AskResult(query_id, Outcome.PROVIDER_ERROR, PROVIDER_ERROR_MESSAGE, False)

    def _finish(
        self,
        caller: Caller,
        query_id: str,
        outcome: Outcome,
        started: float,
        sources: list[Source],
        citations: list[Citation],
        top_score: float,
        tokens: tuple[int, int] = (0, 0),
    ) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        self._metric("QueryLatency", latency_ms, "Milliseconds")
        cited = list(dict.fromkeys(c.document_id for c in citations))
        self.publisher.publish(
            DetailType.QUESTION_ANSWERED,
            school_id=caller.school_id,
            correlation_id=query_id,
            data={
                "queryId": query_id,
                "askerId": caller.user_id,
                "askerRole": str(caller.role),
                "outcome": str(outcome),
                "chunksRetrieved": len(sources),
                "citedDocumentIds": cited,
                "topScore": round(top_score, 3),
                "latencyMs": latency_ms,
                "embeddingModel": self.settings.embedding_model_id,
                "generationProvider": self.settings.generation_provider,
                "generationModel": self.settings.generation_model,
                "inputTokens": tokens[0],
                "outputTokens": tokens[1],
            },
        )

    # --- POST /feedback ----------------------------------------------------------------

    def feedback(
        self, caller: Caller, query_id: str, helpful: bool, reason_code: str | None
    ) -> None:
        if self.query_log.asker_of(query_id) != caller.user_id:
            raise FeedbackNotAllowedError(query_id)
        data: dict[str, Any] = {"queryId": query_id, "helpful": helpful}
        if reason_code:
            data["reasonCode"] = reason_code
        self.publisher.publish(
            DetailType.ANSWER_FEEDBACK_GIVEN,
            school_id=caller.school_id,
            correlation_id=query_id,
            data=data,
        )

    # --- admin ------------------------------------------------------------------------

    def list_documents(self, caller: Caller) -> list[dict[str, Any]]:
        return [
            {
                "documentId": d.document_id,
                "title": d.title,
                "audience": list(d.audience),
                "status": d.status,
                "activeVersion": d.active_version,
                "updatedAt": d.updated_at,
            }
            for d in self.registry.list_documents(caller.school_id)
        ]

    def retire(self, caller: Caller, document_id: str) -> dict[str, Any]:
        """Take a document out of search. The registry changes first, so the document
        stops being used at once even if the cleanup that follows is interrupted."""
        document = self.registry.get_document(caller.school_id, document_id)
        if document is None or not document.active_version:
            raise DocumentNotFoundError(document_id)
        version = document.active_version
        try:
            self.registry.retire(
                school_id=caller.school_id, document_id=document_id, version=version, now=self.now()
            )
        except RetireConflictError:
            raise DocumentNotFoundError(document_id) from None
        self.cleaner.discard(caller.school_id, document_id, version)
        self.publisher.publish(
            DetailType.DOCUMENT_RETIRED,
            school_id=caller.school_id,
            correlation_id=document_id,
            data={"documentId": document_id, "version": version, "retiredBy": caller.user_id},
        )
        self.logger.info("document retired", document_id=document_id, version=version)
        return {"documentId": document_id, "version": version, "status": Status.RETIRED.value}

    def _metric(self, name: str, value: float, unit: str = "Count") -> None:
        self.metrics.record(name, value, unit, {})


def _source(source_id: str, hit: QueryHit) -> Source:
    meta = hit.metadata
    return Source(
        source_id=source_id,
        document_id=str(meta.get("document_id", "")),
        title=str(meta.get("title", "")),
        text=str(meta.get("chunk_text", "")),
        heading_path=str(meta.get("heading_path", "")),
        page_start=int(meta["page_start"]) if "page_start" in meta else None,
        page_end=int(meta["page_end"]) if "page_end" in meta else None,
    )

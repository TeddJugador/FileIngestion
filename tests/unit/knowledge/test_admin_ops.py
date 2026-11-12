from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from ingestion.replay import DlqOperations
from knowledge.admin_ops import (
    REQUEST_ADAPTER,
    AdminOps,
    ReindexRequest,
    describe_indexing_message,
)
from knowledge.indexer import Outcome
from knowledge.jobs import JobKind, parse_job
from knowledge.vector_store import InMemoryVectorStore
from tests.unit.aws import REGION
from tests.unit.knowledge.indexing_harness import INDEX, KnowledgeHarness
from tests.unit.knowledge.query_harness import index_text

NEW_INDEX = "docs-gemini-768-v2"
DOC = "# Fees\n\n## Due dates\n\nSchool fees are due on the seventh day of every month."


class Queues:
    def __init__(self) -> None:
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.dlq_url = self.sqs.create_queue(QueueName="document-indexing-dlq")["QueueUrl"]
        self.dlq_arn = self._arn(self.dlq_url)
        self.url = self.sqs.create_queue(
            QueueName="document-indexing-queue",
            Attributes={
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": self.dlq_arn, "maxReceiveCount": 3}
                )
            },
        )["QueueUrl"]
        self.arn = self._arn(self.url)

    def _arn(self, url: str) -> str:
        return self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]

    def bodies(self) -> list[str]:
        messages = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10).get(
            "Messages", []
        )
        return [m["Body"] for m in messages]


def _ops(
    kh: KnowledgeHarness,
    queues: Queues,
    index_name: str = NEW_INDEX,
    sleeps: list[float] | None = None,
) -> AdminOps:
    recorded = sleeps if sleeps is not None else []
    return AdminOps(
        registry=kh.registry,
        sqs=queues.sqs,
        queue_url=queues.url,
        index_name=index_name,
        dlq=DlqOperations(
            queues.sqs,
            dlq_url=queues.dlq_url,
            dlq_arn=queues.dlq_arn,
            main_queue_arn=queues.arn,
            describe_body=describe_indexing_message,
        ),
        logger=kh.logger,
        sleep=recorded.append,
    )


@pytest.fixture
def setup(kh: KnowledgeHarness) -> tuple[KnowledgeHarness, Queues]:
    index_text(kh, DOC, document_id="doc-aaaaaaaaaaa1", audience=("PARENT",), title="Fees")
    index_text(
        kh,
        DOC + " Late fees apply.",
        document_id="doc-aaaaaaaaaaa2",
        audience=("STAFF",),
        title="Staff fees",
    )
    index_text(
        kh,
        DOC,
        document_id="doc-bbbbbbbbbbb1",
        audience=("PARENT",),
        title="Fees",
        school_id="SCH-002",
    )
    return kh, Queues()


def test_reindex_queues_every_active_document_of_a_school(setup: Any) -> None:
    kh, queues = setup
    result = _ops(kh, queues).reindex(ReindexRequest(action="reindex", schoolId="SCH-001"))
    assert result["queued"] == 2
    assert result["indexName"] == NEW_INDEX
    jobs = [parse_job(b) for b in queues.bodies()]
    assert {j.document_id for j in jobs} == {"doc-aaaaaaaaaaa1", "doc-aaaaaaaaaaa2"}
    assert all(j.kind is JobKind.REINDEX for j in jobs)
    assert all(
        j.source_key.startswith(f"sources/SCH-001/{j.document_id}/{j.version}/") for j in jobs
    )


def test_reindex_all_schools(setup: Any) -> None:
    kh, queues = setup
    result = _ops(kh, queues).reindex(ReindexRequest(action="reindex"))
    assert result["queued"] == 3


def test_dry_run_sends_nothing(setup: Any) -> None:
    kh, queues = setup
    result = _ops(kh, queues).reindex(ReindexRequest(action="reindex", dryRun=True))
    assert (result["queued"], result["dryRun"]) == (3, True)
    assert queues.bodies() == []


def test_documents_already_in_the_index_are_skipped(setup: Any) -> None:
    kh, queues = setup
    result = _ops(kh, queues, index_name=INDEX).reindex(ReindexRequest(action="reindex"))
    assert (result["queued"], result["alreadyInIndex"]) == (0, 3)


def test_reindex_is_throttled(setup: Any) -> None:
    kh, queues = setup
    sleeps: list[float] = []
    _ops(kh, queues, sleeps=sleeps).reindex(ReindexRequest(action="reindex", ratePerSecond=4))
    assert sleeps == [0.25, 0.25]  # between the 3 messages, none before the first


def test_retired_documents_are_not_reindexed(setup: Any) -> None:
    kh, queues = setup
    from tests.unit.knowledge.query_harness import ADMIN, create_query_log, query_service

    create_query_log()
    query_service(kh).retire(ADMIN, "doc-aaaaaaaaaaa1")
    for school in ("SCH-001", None):  # the index lookup and the all-schools scan
        result = _ops(kh, queues).reindex(
            ReindexRequest(action="reindex", schoolId=school, dryRun=True)
        )
        assert "doc-aaaaaaaaaaa1" not in result["documents"]
        # Retired isn't "missing": it mustn't show up as a problem in the report either.
        assert result["missingSource"] == []


def test_queued_message_is_reindexed_by_the_real_indexer(setup: Any) -> None:
    kh, queues = setup
    _ops(kh, queues).reindex(ReindexRequest(action="reindex", schoolId="SCH-001"))
    new_store = InMemoryVectorStore()
    indexer = kh.indexer(index_name=NEW_INDEX, store=new_store)
    outcomes = {indexer.process(parse_job(body)).outcome for body in queues.bodies()}
    assert outcomes == {Outcome.REINDEXED}
    assert new_store.keys_for("doc-aaaaaaaaaaa1")
    document = kh.registry.get_document("SCH-001", "doc-aaaaaaaaaaa1")
    assert document is not None
    assert document.index_name == NEW_INDEX  # the query path's model check follows it
    # Asking again finds nothing left to do.
    again = _ops(kh, queues).reindex(ReindexRequest(action="reindex", schoolId="SCH-001"))
    assert (again["queued"], again["alreadyInIndex"]) == (0, 2)


# --- DLQ --------------------------------------------------------------------------------


def test_inspect_describes_indexing_messages(setup: Any) -> None:
    kh, queues = setup
    reindex = {
        "kind": "REINDEX",
        "schoolId": "SCH-001",
        "documentId": "doc-aaaaaaaaaaa1",
        "version": "v-0123456789ab",
        "sourceKey": "sources/SCH-001/doc-aaaaaaaaaaa1/v-0123456789ab/source.md",
        "title": "Fees",
        "audience": ["PARENT"],
        "importId": "imp-1",
    }
    queues.sqs.send_message(QueueUrl=queues.dlq_url, MessageBody=json.dumps(reindex))
    queues.sqs.send_message(QueueUrl=queues.dlq_url, MessageBody="garbage")
    result = _ops(kh, queues).handle(REQUEST_ADAPTER.validate_python({"action": "inspect_dlq"}))
    messages = sorted(result["messages"], key=lambda m: "unparseableBody" in m)
    assert messages[0]["documentId"] == "doc-aaaaaaaaaaa1"
    assert messages[0]["kind"] == "REINDEX"
    assert messages[1]["unparseableBody"] is True
    assert result["action"] == "inspect_dlq"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "reindex", "schoolId": "school-1"},
        {"action": "reindex", "ratePerSecond": 50},
        {"action": "reindex", "dryRun": "yes"},
        {"action": "inspect_dlq", "max": 0},
        {"action": "explode"},
        {},
    ],
)
def test_invalid_payloads_are_rejected(payload: dict[str, Any]) -> None:
    from knowledge.handlers.admin_ops import InvalidRequestError, handler
    from tests.unit.test_handler import FakeContext

    with pytest.raises(InvalidRequestError, match="Expected one of"):
        handler(payload, FakeContext())

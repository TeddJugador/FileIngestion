"""Manual operations: re-index into the configured index, and the indexing DLQ.

Re-indexing sends internal ``REINDEX`` messages straight to document-indexing-queue, not
a synthetic ``DocumentAccepted`` to the bus: only ingestion may publish with source
``school.ingestion``, and the staged copy an old event points to has usually expired.
A re-index reads this service's own copy of each source instead.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from aws_lambda_powertools import Logger
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, TypeAdapter

from ingestion.replay import DlqOperations
from knowledge.jobs import parse_job
from knowledge.registry import Registry


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReindexRequest(_Request):
    action: Literal["reindex"]
    schoolId: Annotated[str, StringConstraints(pattern=r"^SCH-\d{3}$")] | None = None
    ratePerSecond: Annotated[float, Field(gt=0, le=10)] | None = None
    dryRun: StrictBool = False


class InspectDlqRequest(_Request):
    action: Literal["inspect_dlq"]
    max: Annotated[int, Field(ge=1, le=100)] = 10


class RedriveDlqRequest(_Request):
    action: Literal["redrive_dlq"]
    maxPerSecond: Annotated[int, Field(ge=1, le=500)] = 10


class DlqStatusRequest(_Request):
    action: Literal["dlq_status"]
    taskHandle: Annotated[str, Field(min_length=1)] | None = None


AdminRequest = ReindexRequest | InspectDlqRequest | RedriveDlqRequest | DlqStatusRequest
REQUEST_ADAPTER: TypeAdapter[AdminRequest] = TypeAdapter(
    Annotated[AdminRequest, Field(discriminator="action")]
)


def describe_indexing_message(body: str) -> dict[str, Any]:
    """The indexing DLQ holds DocumentAccepted events and REINDEX requests."""
    job = parse_job(body)  # InvalidJobError (a ValueError) marks the body unparseable
    return {
        "kind": str(job.kind),
        "schoolId": job.school_id,
        "documentId": job.document_id,
        "version": job.version,
    }


@dataclass
class AdminOps:
    registry: Registry
    sqs: Any  # boto3 SQS client
    queue_url: str
    index_name: str
    dlq: DlqOperations
    logger: Logger
    default_rate: float = 2.0
    sleep: Callable[[float], None] = time.sleep

    def handle(self, request: AdminRequest) -> dict[str, Any]:
        if isinstance(request, ReindexRequest):
            return self.reindex(request)
        if isinstance(request, InspectDlqRequest):
            return {**self.dlq.inspect(request.max), "action": "inspect_dlq"}
        if isinstance(request, RedriveDlqRequest):
            return {**self.dlq.redrive(request.maxPerSecond), "action": "redrive_dlq"}
        return {**self.dlq.status(request.taskHandle), "action": "dlq_status"}

    def reindex(self, request: ReindexRequest) -> dict[str, Any]:
        rate = request.ratePerSecond or self.default_rate
        queued: list[str] = []
        already: list[str] = []
        missing_source: list[str] = []
        for document in self.registry.active_documents(request.schoolId):
            if document.index_name == self.index_name:
                already.append(document.document_id)  # nothing to do
                continue
            version = (
                self.registry.get_version(
                    document.school_id, document.document_id, document.active_version
                )
                if document.active_version
                else None
            )
            if version is None or not version.source_key:
                missing_source.append(document.document_id)
                continue
            message = {
                "kind": "REINDEX",
                "schoolId": document.school_id,
                "documentId": document.document_id,
                "version": version.version,
                "sourceKey": version.source_key,
                "title": document.title,
                "audience": list(document.audience),
                "importId": version.import_id,
            }
            if not request.dryRun:
                if queued:
                    self.sleep(1 / rate)  # keep embedding calls inside provider limits
                self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(message))
            queued.append(document.document_id)
        self.logger.info(
            "reindex requested",
            school_id=request.schoolId or "ALL",
            queued=len(queued),
            already_in_index=len(already),
            missing_source=len(missing_source),
            dry_run=request.dryRun,
        )
        return {
            "action": "reindex",
            "indexName": self.index_name,
            "dryRun": request.dryRun,
            "queued": len(queued),
            "alreadyInIndex": len(already),
            "missingSource": missing_source,
            "documents": queued,
        }

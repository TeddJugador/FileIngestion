"""Knowledge-service events on SchoolEventBus (source ``school.knowledge``)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from ulid import ULID

SOURCE = "school.knowledge"
PRODUCER = "knowledge-service"
SCHEMA_VERSION = "1.0"


class DetailType(StrEnum):
    DOCUMENT_INDEXED = "DocumentIndexed"
    DOCUMENT_INDEXING_FAILED = "DocumentIndexingFailed"
    DOCUMENT_RETIRED = "DocumentRetired"
    QUESTION_ANSWERED = "QuestionAnswered"
    ANSWER_FEEDBACK_GIVEN = "AnswerFeedbackGiven"


class EventPublishError(Exception):
    pass


class Publisher(Protocol):
    def publish(
        self,
        detail_type: DetailType,
        *,
        school_id: str,
        correlation_id: str,
        data: Mapping[str, Any],
    ) -> str: ...


def iso_ms(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventPublisher:
    def __init__(
        self,
        client: Any,  # noqa: ANN401 - boto3 events client
        bus_name: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_event_id: Callable[[], str] = lambda: str(ULID()),
    ) -> None:
        self._client = client
        self._bus = bus_name
        self._now = now
        self._new_event_id = new_event_id

    def publish(
        self,
        detail_type: DetailType,
        *,
        school_id: str,
        correlation_id: str,
        data: Mapping[str, Any],
    ) -> str:
        event_id = self._new_event_id()
        detail = {
            "metadata": {
                "eventId": event_id,
                "schemaVersion": SCHEMA_VERSION,
                "occurredAt": iso_ms(self._now()),
                "correlationId": correlation_id,
                "schoolId": school_id,
                "producer": PRODUCER,
            },
            "data": dict(data),
        }
        response = self._client.put_events(
            Entries=[
                {
                    "Source": SOURCE,
                    "DetailType": str(detail_type),
                    "Detail": json.dumps(detail),
                    "EventBusName": self._bus,
                }
            ]
        )
        if response.get("FailedEntryCount", 0):
            raise EventPublishError(f"put_events failed for {detail_type}")
        return event_id

"""Event envelope (EVENT_CATALOGUE.md section 1) and the EventBridge publisher."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ulid import ULID

from ingestion.clock import iso_ms

if TYPE_CHECKING:
    from mypy_boto3_events import EventBridgeClient

SOURCE = "school.ingestion"
PRODUCER = "ingestion-service"
SCHEMA_VERSION = "1.0"


class DetailType(StrEnum):
    IMPORT_COMPLETED = "ImportCompleted"
    IMPORT_FAILED = "ImportFailed"
    DOCUMENT_ACCEPTED = "DocumentAccepted"


class EventPublishError(Exception):
    """EventBridge rejected the entry; the message must be retried."""


def build_detail(
    *,
    school_id: str,
    correlation_id: str,
    occurred_at: datetime,
    data: Mapping[str, Any],
    event_id: str,
) -> dict[str, Any]:
    return {
        "metadata": {
            "eventId": event_id,
            "schemaVersion": SCHEMA_VERSION,
            "occurredAt": iso_ms(occurred_at),
            "correlationId": correlation_id,
            "schoolId": school_id,
            "producer": PRODUCER,
        },
        "data": dict(data),
    }


class EventPublisher:
    def __init__(
        self,
        client: EventBridgeClient,
        bus_name: str,
        *,
        new_event_id: Callable[[], str] = lambda: str(ULID()),
    ) -> None:
        self._client = client
        self._bus = bus_name
        self._new_event_id = new_event_id

    def publish(
        self,
        detail_type: DetailType,
        *,
        school_id: str,
        correlation_id: str,
        occurred_at: datetime,
        data: Mapping[str, Any],
    ) -> str:
        """Publish one event and return its ``eventId``. Raises on any failed entry."""
        event_id = self._new_event_id()
        detail = build_detail(
            school_id=school_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            data=data,
            event_id=event_id,
        )
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
            codes = [e.get("ErrorCode") for e in response.get("Entries", []) if e.get("ErrorCode")]
            raise EventPublishError(f"put_events failed: {codes}")
        return event_id

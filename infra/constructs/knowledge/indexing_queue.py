"""DocumentAccepted -> document-indexing-queue (with DLQ), via an EventBridge rule."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.knowledge.limits import (
    DLQ_RETENTION,
    INDEXER_TIMEOUT,
    INDEXING_MAX_RECEIVE_COUNT,
)


class IndexingQueue(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, event_bus: events.IEventBus) -> None:
        super().__init__(scope, construct_id)

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name="document-indexing-dlq",
            retention_period=DLQ_RETENTION,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self,
            "Queue",
            queue_name="document-indexing-queue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=Duration.seconds(INDEXER_TIMEOUT.to_seconds() * 6),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dead_letter_queue, max_receive_count=INDEXING_MAX_RECEIVE_COUNT
            ),
        )

        # Event catalogue: every rule target has its own DLQ and a retry policy. Events
        # EventBridge can't deliver land in the same DLQ as messages the indexer failed
        # on; both carry the original event as the body, so one redrive handles both.
        self.rule = events.Rule(
            self,
            "DocumentAcceptedRule",
            event_bus=event_bus,
            description="DocumentAccepted from ingestion -> knowledge indexing queue",
            event_pattern=events.EventPattern(
                source=["school.ingestion"], detail_type=["DocumentAccepted"]
            ),
            targets=[
                targets.SqsQueue(
                    self.queue,
                    dead_letter_queue=self.dead_letter_queue,
                    retry_attempts=185,
                    max_event_age=Duration.hours(24),
                )
            ],
        )

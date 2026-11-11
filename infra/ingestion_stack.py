"""File ingestion service stack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.constructs.dependencies_layer import DependenciesLayer
from infra.constructs.ingestion_bucket import (
    IngestionBucket,
    bucket_arn_for_name,
    ingestion_bucket_name,
)
from infra.constructs.ops_alarms import OpsAlarms
from infra.constructs.processing_queue import ProcessingQueues
from infra.constructs.replay_function import ReplayFunction
from infra.constructs.staging_tables import StagingTables
from infra.constructs.worker_function import WORKER_TIMEOUT, WorkerFunction


class IngestionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        event_bus: events.IEventBus,
        ops_alerts_topic: sns.ITopic,
        layer_asset_path: str | Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.event_bus = event_bus
        self.ops_alerts_topic = ops_alerts_topic

        self.queues = ProcessingQueues(self, "Processing", worker_timeout=WORKER_TIMEOUT)

        # The bucket ARN is derived from its name, not from the bucket resource, so the
        # queue policy doesn't reference the bucket and the bucket can depend on the
        # queue policy without a circular dependency.
        bucket_name = ingestion_bucket_name(self)
        self.storage = IngestionBucket(
            self,
            "Storage",
            bucket_name=bucket_name,
            notification_destination=self.queues.notification_destination(
                source_bucket_arn=bucket_arn_for_name(self, bucket_name)
            ),
        )

        self.dependencies_layer = DependenciesLayer(
            self, "Dependencies", asset_path=layer_asset_path
        ).layer

        self.tables = StagingTables(self, "Staging")

        self.worker = WorkerFunction(
            self,
            "EtlWorker",
            bucket=self.storage.bucket,
            queue=self.queues.queue,
            records_table=self.tables.records,
            idempotency_table=self.tables.idempotency,
            event_bus=event_bus,
            layer=self.dependencies_layer,
        )

        self.replay = ReplayFunction(
            self,
            "DlqReplay",
            dead_letter_queue=self.queues.dead_letter_queue,
            main_queue=self.queues.queue,
            layer=self.dependencies_layer,
        )

        self.alarms = OpsAlarms(
            self,
            "Alarms",
            dead_letter_queue=self.queues.dead_letter_queue,
            main_queue=self.queues.queue,
            worker=self.worker.function,
            topic=ops_alerts_topic,
        )

        # Plain outputs (no export names) for scripts and the runbook.
        CfnOutput(self, "BucketName", value=self.storage.bucket.bucket_name)
        CfnOutput(self, "WorkerFunctionName", value=self.worker.function.function_name)
        CfnOutput(self, "DlqReplayFunctionName", value=self.replay.function.function_name)
        CfnOutput(self, "DeadLetterQueueUrl", value=self.queues.dead_letter_queue.queue_url)

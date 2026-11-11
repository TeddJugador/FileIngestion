"""The etl_worker Lambda, its least-privilege role and its SQS trigger."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Stack, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import (
    LAMBDA_ARCHITECTURE,
    LAMBDA_RUNTIME,
    PROJECT_ROOT,
    PYTHON_VERSION,
)

WORKER_TIMEOUT = Duration.seconds(300)
WORKER_MEMORY_MB = 1024
MAX_CONCURRENCY = 5
MAX_TABULAR_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
SOURCE_DIR = PROJECT_ROOT / "src"


def lambda_code() -> lambda_.Code:
    return lambda_.Code.from_asset(
        str(SOURCE_DIR),
        exclude=["**/__pycache__", "**/*.pyc", "*.egg-info", "**/*.egg-info"],
    )


def log_permissions(role: iam.Role, log_group: logs.ILogGroup) -> None:
    """CloudWatch Logs for this function's own log group only (no managed policy)."""
    role.add_to_policy(
        iam.PolicyStatement(
            sid="WriteOwnLogs",
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[log_group.log_group_arn],
        )
    )


RUNTIME_REASON = (
    f"python{PYTHON_VERSION} is the newest runtime that is GA on Lambda and supported by "
    "Powertools at build time; python3.15 is a Lambda public preview not yet supported "
    "by Powertools."
)


class WorkerFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket: s3.IBucket,
        queue: sqs.IQueue,
        records_table: dynamodb.ITable,
        idempotency_table: dynamodb.ITable,
        event_bus: events.IEventBus,
        layer: lambda_.ILayerVersion,
    ) -> None:
        super().__init__(scope, construct_id)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="etl_worker: least privilege, no managed policies",
        )
        log_permissions(self.role, self.log_group)
        self._grant(bucket, records_table, idempotency_table, event_bus)

        self.function = lambda_.Function(
            self,
            "Function",
            description="Validates and stages bulk uploads, then announces the result",
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="ingestion.handlers.etl_worker.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=WORKER_MEMORY_MB,
            timeout=WORKER_TIMEOUT,
            tracing=lambda_.Tracing.ACTIVE,
            role=self.role,
            log_group=self.log_group,
            environment={
                "RECORDS_TABLE_NAME": records_table.table_name,
                "IDEMPOTENCY_TABLE_NAME": idempotency_table.table_name,
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "BUCKET_NAME": bucket.bucket_name,
                "MAX_TABULAR_BYTES": str(MAX_TABULAR_BYTES),
                "MAX_DOCUMENT_BYTES": str(MAX_DOCUMENT_BYTES),
                "POWERTOOLS_SERVICE_NAME": "ingestion",
                "POWERTOOLS_METRICS_NAMESPACE": "SchoolSystem/Ingestion",
            },
        )

        # Backpressure via the event source mapping's maximum concurrency, not reserved
        # concurrency: reserved concurrency throttles the poller, and throttled messages
        # burn receive attempts and can reach the DLQ without ever failing.
        self.function.add_event_source(
            sources.SqsEventSource(
                queue,
                batch_size=1,
                report_batch_item_failures=True,
                max_concurrency=MAX_CONCURRENCY,
            )
        )

        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )

    def _grant(
        self,
        bucket: s3.IBucket,
        records_table: dynamodb.ITable,
        idempotency_table: dynamodb.ITable,
        event_bus: events.IEventBus,
    ) -> None:
        raw_objects = bucket.arn_for_objects("raw/*")
        processed_objects = bucket.arn_for_objects("processed/*")
        statements = [
            # GetObjectVersion: every read, head and copy names a VersionId.
            iam.PolicyStatement(
                sid="ReadRawUploads",
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[raw_objects],
            ),
            # PutObject covers create/upload-part/complete; abort is separate.
            iam.PolicyStatement(
                sid="WriteProcessedOutput",
                actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                resources=[processed_objects],
            ),
            iam.PolicyStatement(
                sid="StageRecords",
                actions=["dynamodb:BatchWriteItem"],
                resources=[records_table.table_arn],
            ),
            iam.PolicyStatement(
                sid="IdempotencyRecords",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[idempotency_table.table_arn],
            ),
            iam.PolicyStatement(
                sid="PublishEvents",
                actions=["events:PutEvents"],
                resources=[event_bus.event_bus_arn],
            ),
        ]
        for statement in statements:
            self.role.add_to_policy(statement)

        bucket_id = Stack.of(self).get_logical_id(bucket.node.default_child)  # type: ignore[arg-type]
        policy = self.role.node.find_child("DefaultPolicy")
        # Acknowledged per finding (not the whole rule), so any new wildcard still fails.
        Validations.of(policy).acknowledge(
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::<{bucket_id}.Arn>/raw/*]",
                reason="Prefix scope on this service's own bucket: the worker reads uploads "
                "under raw/.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::<{bucket_id}.Arn>/processed/*]",
                reason="Prefix scope on this service's own bucket: the worker writes "
                "claim-check files under processed/.",
            ),
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="X-Ray PutTraceSegments/PutTelemetryRecords only support Resource '*'.",
            ),
        )

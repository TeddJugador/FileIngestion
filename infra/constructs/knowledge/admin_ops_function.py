"""admin_ops: re-index and indexing-DLQ tools, invoked manually."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.knowledge.limits import ADMIN_SERVICE
from infra.constructs.knowledge.registry import BY_SCHOOL_STATUS_INDEX
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions

REINDEX_RATE_PER_SECOND = 2  # messages queued per second; the indexer itself runs at 2


class AdminOpsFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        registry: dynamodb.ITable,
        queue: sqs.IQueue,
        dead_letter_queue: sqs.IQueue,
        index_name: str,
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
            description="knowledge admin_ops: least privilege, no managed policies",
        )
        log_permissions(self.role, self.log_group)
        for statement in (
            iam.PolicyStatement(
                sid="FindActiveDocuments",
                # Scan only for "all schools"; one school uses the BySchoolStatus index.
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                resources=[
                    registry.table_arn,
                    f"{registry.table_arn}/index/{BY_SCHOOL_STATUS_INDEX}",
                ],
            ),
            iam.PolicyStatement(
                sid="QueueReindexMessages", actions=["sqs:SendMessage"], resources=[queue.queue_arn]
            ),
            iam.PolicyStatement(
                sid="InspectAndRedriveDlq",
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes",
                    "sqs:StartMessageMoveTask",
                    "sqs:ListMessageMoveTasks",
                    # The move task runs as the caller and deletes from the source queue.
                    "sqs:DeleteMessage",
                ],
                resources=[dead_letter_queue.queue_arn],
            ),
        ):
            self.role.add_to_policy(statement)

        self.function = lambda_.Function(
            self,
            "Function",
            description=(
                'Knowledge admin: {"action": "reindex" | "inspect_dlq" | "redrive_dlq" | '
                '"dlq_status"}'
            ),
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="knowledge.handlers.admin_ops.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=256,
            timeout=Duration.minutes(15),  # throttled re-index of a large school
            role=self.role,
            log_group=self.log_group,
            environment={
                "REGISTRY_TABLE_NAME": registry.table_name,
                "INDEXING_QUEUE_URL": queue.queue_url,
                "INDEXING_QUEUE_ARN": queue.queue_arn,
                "INDEXING_DLQ_URL": dead_letter_queue.queue_url,
                "INDEXING_DLQ_ARN": dead_letter_queue.queue_arn,
                "VECTOR_INDEX_NAME": index_name,
                "REINDEX_RATE_PER_SECOND": str(REINDEX_RATE_PER_SECOND),
                "POWERTOOLS_SERVICE_NAME": ADMIN_SERVICE,
            },
        )
        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )

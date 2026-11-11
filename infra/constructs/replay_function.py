"""dlq_replay: an admin tool, invoked manually, to inspect and redrive the DLQ."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Validations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions


class ReplayFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        dead_letter_queue: sqs.IQueue,
        main_queue: sqs.IQueue,
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
            description="dlq_replay: least privilege, no managed policies",
        )
        log_permissions(self.role, self.log_group)
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="InspectAndRedriveDlq",
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes",
                    "sqs:StartMessageMoveTask",
                    "sqs:ListMessageMoveTasks",
                    # StartMessageMoveTask runs as the caller: SQS receives from and
                    # deletes on the source queue, so the task fails without this.
                    "sqs:DeleteMessage",
                ],
                resources=[dead_letter_queue.queue_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="RedriveToMainQueue",
                actions=["sqs:SendMessage"],
                resources=[main_queue.queue_arn],
            )
        )

        self.function = lambda_.Function(
            self,
            "Function",
            description='DLQ tool: {"action": "inspect" | "redrive" | "status", ...}',
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="ingestion.handlers.dlq_replay.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=256,
            timeout=Duration.seconds(60),
            role=self.role,
            log_group=self.log_group,
            environment={
                "DLQ_URL": dead_letter_queue.queue_url,
                "DLQ_ARN": dead_letter_queue.queue_arn,
                "MAIN_QUEUE_ARN": main_queue.queue_arn,
                "POWERTOOLS_SERVICE_NAME": "ingestion-dlq-replay",
            },
        )
        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )

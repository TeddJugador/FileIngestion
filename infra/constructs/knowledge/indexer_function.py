"""The indexer Lambda: DocumentAccepted / REINDEX -> chunks -> vectors."""

from __future__ import annotations

from aws_cdk import Acknowledgment, ArnFormat, RemovalPolicy, Stack, Validations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.ingestion_bucket import bucket_arn_for_name, ingestion_bucket_name
from infra.constructs.knowledge.limits import (
    INDEXER_MAX_CONCURRENCY,
    INDEXER_MEMORY_MB,
    INDEXER_SERVICE,
    INDEXER_TIMEOUT,
    INDEXING_MAX_RECEIVE_COUNT,
    METRICS_NAMESPACE,
)
from infra.constructs.knowledge.vector_store import VectorStore
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions
from infra.knowledge_config import KnowledgeConfig

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # enforced by ingestion; asserted again here
VECTOR_PUT_BATCH_SIZE = 500  # S3 Vectors maximum per PutVectors call


def provider_secret_arn(scope: Construct, secret_name: str) -> str:
    """Secrets Manager appends a random 6-character suffix to every secret's ARN."""
    return Stack.of(scope).format_arn(
        service="secretsmanager",
        resource="secret",
        resource_name=f"{secret_name}-??????",
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )


EMBEDDING_SECRETS = {"gemini": "knowledge/gemini", "voyage": "knowledge/voyage"}


class IndexerFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: KnowledgeConfig,
        queue: sqs.IQueue,
        knowledge_bucket: s3.IBucket,
        vectors: VectorStore,
        registry: dynamodb.ITable,
        idempotency: dynamodb.ITable,
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
            description="knowledge indexer: least privilege, no managed policies",
        )
        log_permissions(self.role, self.log_group)
        # The ingestion bucket's name is deterministic, so its ARN is built from the name
        # (as the ingestion stack's own queue policy does) instead of importing it from
        # that stack. The stack-level dependency keeps the deploy order.
        self.ingestion_bucket_name = ingestion_bucket_name(self)
        self.ingestion_bucket_arn = bucket_arn_for_name(self, self.ingestion_bucket_name)
        self.embedding_secret_arn = provider_secret_arn(
            self, EMBEDDING_SECRETS[config.embedding_provider]
        )
        statements = [
            # Only the documents prefix of the ingestion bucket: the other processed/
            # prefixes hold learners' personal data. Source of a server-side copy.
            iam.PolicyStatement(
                sid="ReadStagedDocuments",
                actions=["s3:GetObject"],
                resources=[f"{self.ingestion_bucket_arn}/processed/documents/*"],
            ),
            iam.PolicyStatement(
                sid="OwnSourcesAndKeyLists",
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[
                    knowledge_bucket.arn_for_objects("sources/*"),
                    knowledge_bucket.arn_for_objects("vector-keys/*"),
                ],
            ),
            iam.PolicyStatement(
                sid="WriteVectors",
                actions=["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors"],
                resources=[vectors.index_arn],
            ),
            iam.PolicyStatement(
                sid="DocumentRegistry",
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[registry.table_arn],
            ),
            iam.PolicyStatement(
                sid="IdempotencyRecords",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[idempotency.table_arn],
            ),
            iam.PolicyStatement(
                sid="EmbeddingProviderKey",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.embedding_secret_arn],
            ),
            iam.PolicyStatement(
                sid="PublishEvents",
                actions=["events:PutEvents"],
                resources=[event_bus.event_bus_arn],
            ),
        ]
        for statement in statements:
            self.role.add_to_policy(statement)

        self.function = lambda_.Function(
            self,
            "Function",
            description="Indexes school documents: extract, chunk, embed, store vectors",
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="knowledge.handlers.indexer.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=INDEXER_MEMORY_MB,
            timeout=INDEXER_TIMEOUT,
            tracing=lambda_.Tracing.ACTIVE,
            role=self.role,
            log_group=self.log_group,
            environment={
                "KNOWLEDGE_BUCKET_NAME": knowledge_bucket.bucket_name,
                "INGESTION_BUCKET_NAME": self.ingestion_bucket_name,
                "VECTOR_BUCKET_NAME": vectors.bucket_name,
                "VECTOR_INDEX_NAME": vectors.index_name,
                "VECTOR_PUT_BATCH_SIZE": str(VECTOR_PUT_BATCH_SIZE),
                "REGISTRY_TABLE_NAME": registry.table_name,
                "IDEMPOTENCY_TABLE_NAME": idempotency.table_name,
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "EMBEDDING_PROVIDER": config.embedding_provider,
                "EMBEDDING_MODEL": config.embedding_model,
                "EMBEDDING_DIMENSION": str(config.embedding_dimension),
                "MAX_DOCUMENT_BYTES": str(MAX_DOCUMENT_BYTES),
                "MAX_RECEIVE_COUNT": str(INDEXING_MAX_RECEIVE_COUNT),
                "POWERTOOLS_SERVICE_NAME": INDEXER_SERVICE,
                "POWERTOOLS_METRICS_NAMESPACE": METRICS_NAMESPACE,
            },
        )
        # Low concurrency on purpose: keeps embedding calls inside provider rate limits.
        self.function.add_event_source(
            sources.SqsEventSource(
                queue,
                batch_size=1,
                report_batch_item_failures=True,
                max_concurrency=INDEXER_MAX_CONCURRENCY,
            )
        )
        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )
        self._acknowledge_prefix_scopes(knowledge_bucket)

    def _acknowledge_prefix_scopes(self, knowledge_bucket: s3.IBucket) -> None:
        """Acknowledged per finding, so any new wildcard still fails the synth."""
        knowledge = nag_resource_label(self, knowledge_bucket.bucket_arn)
        ingestion = nag_resource_label(self, self.ingestion_bucket_arn)
        Validations.of(self.role.node.find_child("DefaultPolicy")).acknowledge(
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::{ingestion}/processed/documents/*]",
                reason="Read-only prefix scope on the ingestion bucket: staged documents only; "
                "never the prefixes holding learners' personal data.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::{knowledge}/sources/*]",
                reason="Prefix scope on this service's own bucket: its copies of documents.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::{knowledge}/vector-keys/*]",
                reason="Prefix scope on this service's own bucket: per-version vector key lists.",
            ),
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="X-Ray PutTraceSegments/PutTelemetryRecords only support Resource '*'.",
            ),
        )


def nag_resource_label(scope: Construct, value: str) -> str:
    """Render a resource the way cdk-nag prints it inside a finding ID.

    The rendering depends on what is known at synth time: without an account and region
    the ingestion bucket ARN contains <AWS::AccountId> and <AWS::Region>; with them (the
    CLI's normal case) it contains the literal values. Deriving the label from the
    resolved value keeps the acknowledgement correct either way.
    """
    return _render(Stack.of(scope).resolve(value))


def _render(resolved: object) -> str:
    if isinstance(resolved, str):
        return resolved
    if isinstance(resolved, dict):
        if "Ref" in resolved:
            return f"<{resolved['Ref']}>"
        if "Fn::GetAtt" in resolved:
            logical_id, attribute = resolved["Fn::GetAtt"]
            return f"<{logical_id}.{attribute}>"
        if "Fn::Join" in resolved:
            separator, parts = resolved["Fn::Join"]
            return str(separator).join(_render(part) for part in parts)
    raise ValueError(f"cannot render {resolved!r} as a cdk-nag resource label")

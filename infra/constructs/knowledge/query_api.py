"""The query API: HTTP API + Cognito JWT authorizer + one Lambda for every route."""

from __future__ import annotations

import jsii
from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Validations
from aws_cdk import aws_apigateway as rest
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.constructs.dependencies_layer import LAMBDA_ARCHITECTURE, LAMBDA_RUNTIME
from infra.constructs.knowledge.indexer_function import (
    EMBEDDING_SECRETS,
    nag_resource_label,
    provider_secret_arn,
)
from infra.constructs.knowledge.registry import BY_SCHOOL_STATUS_INDEX
from infra.constructs.knowledge.vector_store import VectorStore
from infra.constructs.worker_function import RUNTIME_REASON, lambda_code, log_permissions
from infra.knowledge_config import KnowledgeConfig

QUERY_MEMORY_MB = 1024
QUERY_TIMEOUT = Duration.seconds(25)  # under API Gateway's 30-second integration limit
THROTTLE_BURST = 20
THROTTLE_RATE = 10
GENERATION_SECRETS = {"CLAUDE": "knowledge/anthropic", "GEMINI": "knowledge/gemini"}

# Access logs: who-called-what metadata only. No source IP, no body, no query string.
ACCESS_LOG_FORMAT = rest.AccessLogFormat.custom(
    '{"requestId":"$context.requestId","routeKey":"$context.routeKey",'
    '"status":"$context.status","responseLatency":"$context.responseLatency",'
    '"integrationLatency":"$context.integrationLatency",'
    '"integrationError":"$context.integrationErrorMessage",'
    '"authorizerError":"$context.authorizer.error"}'
)


@jsii.implements(apigw.IAccessLogSettings)
class _AccessLogs:
    def __init__(self, log_group: logs.ILogGroup) -> None:
        self._destination = apigw.LogGroupLogDestination(log_group)

    @property
    def destination(self) -> apigw.IAccessLogDestination:
        return self._destination

    @property
    def format(self) -> rest.AccessLogFormat:
        return ACCESS_LOG_FORMAT


class QueryApi(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: KnowledgeConfig,
        user_pool: cognito.IUserPool,
        user_pool_clients: list[cognito.IUserPoolClient],
        knowledge_bucket: s3.IBucket,
        vectors: VectorStore,
        registry: dynamodb.ITable,
        query_log: dynamodb.ITable,
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
            description="knowledge query API: least privilege, no managed policies",
        )
        log_permissions(self.role, self.log_group)
        secrets = [
            provider_secret_arn(self, EMBEDDING_SECRETS[config.embedding_provider]),
            provider_secret_arn(self, GENERATION_SECRETS[config.generation_provider]),
        ]
        statements = [
            iam.PolicyStatement(
                sid="QueryVectors",
                # QueryVectors needs GetVectors too when filtering or returning metadata.
                actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
                resources=[vectors.index_arn],
            ),
            iam.PolicyStatement(
                sid="RetireDeletesVectors",
                actions=["s3vectors:DeleteVectors"],
                resources=[vectors.index_arn],
            ),
            iam.PolicyStatement(
                sid="RetireDeletesFiles",
                actions=["s3:GetObject", "s3:DeleteObject"],
                resources=[
                    knowledge_bucket.arn_for_objects("sources/*"),
                    knowledge_bucket.arn_for_objects("vector-keys/*"),
                ],
            ),
            iam.PolicyStatement(
                sid="DocumentRegistry",
                actions=[
                    "dynamodb:BatchGetItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    registry.table_arn,
                    f"{registry.table_arn}/index/{BY_SCHOOL_STATUS_INDEX}",
                ],
            ),
            iam.PolicyStatement(
                sid="QueryLog",
                actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[query_log.table_arn],
            ),
            iam.PolicyStatement(
                sid="ProviderKeys",
                actions=["secretsmanager:GetSecretValue"],
                resources=list(dict.fromkeys(secrets)),
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
            description="Knowledge API: grounded answers from school documents",
            runtime=LAMBDA_RUNTIME,
            architecture=LAMBDA_ARCHITECTURE,
            handler="knowledge.handlers.api.handler",
            code=lambda_code(),
            layers=[layer],
            memory_size=QUERY_MEMORY_MB,
            timeout=QUERY_TIMEOUT,
            tracing=lambda_.Tracing.ACTIVE,
            role=self.role,
            log_group=self.log_group,
            environment={
                "KNOWLEDGE_BUCKET_NAME": knowledge_bucket.bucket_name,
                "VECTOR_BUCKET_NAME": vectors.bucket_name,
                "VECTOR_INDEX_NAME": vectors.index_name,
                "REGISTRY_TABLE_NAME": registry.table_name,
                "QUERY_LOG_TABLE_NAME": query_log.table_name,
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "EMBEDDING_PROVIDER": config.embedding_provider,
                "EMBEDDING_MODEL": config.embedding_model,
                "EMBEDDING_DIMENSION": str(config.embedding_dimension),
                "GENERATION_PROVIDER": config.generation_provider,
                "GENERATION_MODEL": config.generation_model,
                "GENERATION_THINKING": config.generation_thinking,
                "GENERATION_EFFORT": config.generation_effort,
                "SIMILARITY_THRESHOLD": "0.35",
                "POWERTOOLS_SERVICE_NAME": "knowledge-api",
                "POWERTOOLS_METRICS_NAMESPACE": "SchoolSystem/Knowledge",
            },
        )
        Validations.of(self.function.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(id="AwsSolutions-L1", reason=RUNTIME_REASON)
        )

        self.api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name="school-knowledge-api",
            description="Questions about school documents (Cognito ID tokens)",
            create_default_stage=False,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=list(config.cors_allowed_origins),
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
            # Every route requires a valid token from this pool, issued to one of its
            # clients. ID tokens: only they carry custom:school_id.
            default_authorizer=authorizers.HttpUserPoolAuthorizer(
                "CognitoAuthorizer", user_pool, user_pool_clients=user_pool_clients
            ),
        )
        self.access_logs = logs.LogGroup(
            self,
            "AccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.stage = apigw.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(burst_limit=THROTTLE_BURST, rate_limit=THROTTLE_RATE),
            access_log_settings=_AccessLogs(self.access_logs),
        )

        integration = integrations.HttpLambdaIntegration("Lambda", self.function)
        for path, method in (
            ("/ask", apigw.HttpMethod.POST),
            ("/feedback", apigw.HttpMethod.POST),
            ("/documents", apigw.HttpMethod.GET),
            ("/documents/{documentId}/retire", apigw.HttpMethod.POST),
        ):
            self.api.add_routes(path=path, methods=[method], integration=integration)

        knowledge = nag_resource_label(self, knowledge_bucket.bucket_arn)
        Validations.of(self.role.node.find_child("DefaultPolicy")).acknowledge(
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::{knowledge}/sources/*]",
                reason="Prefix scope on this service's own bucket: retire deletes a source copy.",
            ),
            Acknowledgment(
                id=f"AwsSolutions-IAM5[Resource::{knowledge}/vector-keys/*]",
                reason="Prefix scope on this service's own bucket: retire reads and deletes a "
                "version's key list.",
            ),
            Acknowledgment(
                id="AwsSolutions-IAM5[Resource::*]",
                reason="X-Ray PutTraceSegments/PutTelemetryRecords only support Resource '*'.",
            ),
        )

    @property
    def url(self) -> str:
        return self.stage.url

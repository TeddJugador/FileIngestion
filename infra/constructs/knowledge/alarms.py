"""Knowledge-service alarms -> the shared ops-alerts topic."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.constructs.knowledge.limits import API_SERVICE, INDEXER_SERVICE, METRICS_NAMESPACE
from infra.knowledge_config import KnowledgeConfig

QUERY_P95_LIMIT_MS = 15_000
PROVIDER_ERRORS_LIMIT = 5


def provider_metric_names(config: KnowledgeConfig) -> list[tuple[str, str]]:
    """(service, Provider) pairs that can emit ProviderErrorCount with this config.

    Provider names are the HTTP callers' names: gemini, voyage, claude."""
    generation = "claude" if config.generation_provider == "CLAUDE" else "gemini"
    pairs = [
        (INDEXER_SERVICE, config.embedding_provider),
        (API_SERVICE, config.embedding_provider),
        (API_SERVICE, generation),
    ]
    return list(dict.fromkeys(pairs))


class KnowledgeAlarms(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: KnowledgeConfig,
        dead_letter_queue: sqs.IQueue,
        query_function: lambda_.IFunction,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)

        self.dlq_not_empty = cloudwatch.Alarm(
            self,
            "IndexingDlqNotEmpty",
            alarm_name="knowledge-indexing-dlq-not-empty",
            alarm_description=(
                "A document failed indexing 3 times (or couldn't be delivered). Inspect it with "
                'the admin_ops function: {"action": "inspect_dlq"}.'
            ),
            metric=dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic=cloudwatch.Stats.MAXIMUM
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.query_latency = cloudwatch.Alarm(
            self,
            "QueryLatencyP95",
            alarm_name="knowledge-query-latency-p95",
            alarm_description="p95 of the query Lambda's duration was over 15 s for 15 minutes.",
            metric=query_function.metric_duration(period=Duration.minutes(15), statistic="p95"),
            threshold=QUERY_P95_LIMIT_MS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        provider_metrics = {
            f"m{i}": cloudwatch.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="ProviderErrorCount",
                dimensions_map={"service": service, "Provider": provider},
                statistic=cloudwatch.Stats.SUM,
                period=Duration.minutes(5),
            )
            for i, (service, provider) in enumerate(provider_metric_names(config))
        }
        self.provider_errors = cloudwatch.Alarm(
            self,
            "ProviderErrors",
            alarm_name="knowledge-provider-errors",
            alarm_description=(
                "More than 5 model-provider calls failed (after retries) in 5 minutes: check "
                "the provider's status and the API keys."
            ),
            metric=cloudwatch.MathExpression(
                expression=" + ".join(f"FILL({name}, 0)" for name in provider_metrics),
                using_metrics=provider_metrics,
                label="Provider errors (all providers)",
                period=Duration.minutes(5),
            ),
            threshold=PROVIDER_ERRORS_LIMIT,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.model_mismatch = cloudwatch.Alarm(
            self,
            "EmbeddingModelMismatch",
            alarm_name="knowledge-embedding-model-mismatch",
            alarm_description=(
                "The configured embedding model isn't the one that built the index, so the API "
                "is refusing to answer. Re-index (admin_ops reindex) or fix cdk.json."
            ),
            metric=cloudwatch.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="EmbeddingModelMismatch",
                dimensions_map={"service": API_SERVICE},
                statistic=cloudwatch.Stats.SUM,
                period=Duration.minutes(5),
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        action = actions.SnsAction(topic)
        for alarm in (
            self.dlq_not_empty,
            self.query_latency,
            self.provider_errors,
            self.model_mismatch,
        ):
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)

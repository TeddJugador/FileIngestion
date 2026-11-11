"""Alarms that page ops through the shared ops-alerts topic."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

MAX_MESSAGE_AGE_SECONDS = 900


class OpsAlarms(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        dead_letter_queue: sqs.IQueue,
        main_queue: sqs.IQueue,
        worker: lambda_.IFunction,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)

        self.dlq_not_empty = cloudwatch.Alarm(
            self,
            "DlqNotEmpty",
            alarm_name="ingestion-dlq-not-empty",
            alarm_description=(
                "A file failed processing 3 times and is in file-processing-dlq. "
                'Inspect it with the dlq_replay function: {"action": "inspect"}.'
            ),
            metric=dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic=cloudwatch.Stats.MAXIMUM
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.queue_backlog = cloudwatch.Alarm(
            self,
            "QueueBacklog",
            alarm_name="ingestion-queue-oldest-message-age",
            alarm_description=(
                "The oldest message in file-processing-queue is over 15 minutes old: "
                "the worker is stuck, throttled or failing."
            ),
            metric=main_queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(1), statistic=cloudwatch.Stats.MAXIMUM
            ),
            threshold=MAX_MESSAGE_AGE_SECONDS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.worker_errors = cloudwatch.Alarm(
            self,
            "WorkerErrors",
            alarm_name="ingestion-worker-errors",
            alarm_description=(
                "The etl_worker invocation failed (unexpected error; the message will be "
                "retried). Bad files do not count: they are reported as ImportFailed."
            ),
            metric=worker.metric_errors(period=Duration.minutes(5), statistic=cloudwatch.Stats.SUM),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        action = actions.SnsAction(topic)
        for alarm in (self.dlq_not_empty, self.queue_backlog, self.worker_errors):
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)  # also tell ops when it recovers

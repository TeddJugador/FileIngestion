"""Resources shared by every service: the event bus, its archive and the ops topic."""

from __future__ import annotations

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.constructs.user_directory import UserDirectory

EVENT_BUS_NAME = "SchoolEventBus"
ARCHIVE_RETENTION = Duration.days(30)
OPS_ALERTS_TOPIC_NAME = "ops-alerts"


class SharedStack(Stack):
    """Exposes ``event_bus``, ``ops_alerts_topic``, ``user_pool``, ``web_client`` and
    ``test_client`` (dev stage only) as stack properties.

    Other stacks receive them as constructor props; CDK wires the cross-stack
    references itself (no hand-named CloudFormation exports).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alert_email: str | None = None,
        stage: str = "dev",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.event_bus = events.EventBus(self, "SchoolEventBus", event_bus_name=EVENT_BUS_NAME)
        self.event_archive = self.event_bus.archive(
            "SchoolEventArchive",
            description="30-day replay archive for every event on SchoolEventBus",
            event_pattern=events.EventPattern(account=[self.account]),
            retention=ARCHIVE_RETENTION,
        )

        # cdk-nag AwsSolutions-SNS2 wants SSE on the topic. CloudWatch alarms can't
        # publish to a topic encrypted with the AWS-managed aws/sns key (its key policy
        # can't grant cloudwatch.amazonaws.com), so a customer-managed key is needed.
        self.ops_alerts_key = kms.Key(
            self,
            "OpsAlertsKey",
            description="Encrypts the ops-alerts SNS topic",
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.ops_alerts_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsToPublish",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=["*"],  # in a key policy, "*" means this key
                # No aws:SourceAccount condition: SNS makes this KMS call on CloudWatch's
                # behalf and the request carries no source account, so the condition
                # never matches and alarm notifications are silently dropped. This
                # matches AWS's documented key policy for alarms -> encrypted topics.
            )
        )

        self.ops_alerts_topic = sns.Topic(
            self,
            "OpsAlerts",
            topic_name=OPS_ALERTS_TOPIC_NAME,
            enforce_ssl=True,
            master_key=self.ops_alerts_key,
        )
        # enforce_ssl attaches a topic policy, and a topic policy replaces the default
        # one that would otherwise let CloudWatch publish. Allow alarms explicitly,
        # scoped to this account's alarms (confused-deputy protection).
        self.ops_alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsToPublish",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.ops_alerts_topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": self.format_arn(
                            service="cloudwatch",
                            resource="alarm",
                            resource_name="*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME,
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )

        if alert_email:
            # AWS emails a confirmation link; alerts arrive only after it is clicked.
            self.ops_alerts_topic.add_subscription(subscriptions.EmailSubscription(alert_email))

        # Users for the whole system (the knowledge API is the first consumer).
        self.users = UserDirectory(self, "Users", stage=stage)
        self.user_pool = self.users.user_pool
        self.web_client = self.users.web_client
        self.test_client = self.users.test_client
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "WebClientId", value=self.web_client.user_pool_client_id)
        if self.test_client is not None:
            CfnOutput(self, "TestClientId", value=self.test_client.user_pool_client_id)

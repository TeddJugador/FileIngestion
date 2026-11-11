"""File-processing queue, its DLQ, and the S3 -> SQS notification destination."""

from __future__ import annotations

import jsii
from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk.interfaces import aws_s3 as s3_refs
from constructs import Construct, IDependable

VISIBILITY_TIMEOUT_MULTIPLIER = 6
MAX_RECEIVE_COUNT = 3
DLQ_RETENTION = Duration.days(14)


@jsii.implements(s3.IBucketNotificationDestination)
class S3QueueNotificationDestination:
    """Bucket notification destination with a tighter queue policy than ``SqsDestination``.

    CDK's built-in ``SqsDestination`` only conditions on ``aws:SourceArn``; a bucket ARN
    contains no account ID, so a same-named bucket in another account would match after
    this one was deleted. This destination adds a single statement that requires both
    ``aws:SourceArn`` and ``aws:SourceAccount``, and only grants ``sqs:SendMessage``.

    ``bind`` returns the queue policy as a dependency so the notification configuration
    is only created after S3 is allowed to publish (S3 validates the destination by
    sending ``s3:TestEvent`` when the configuration is written).

    ``source_bucket_arn`` lets the caller supply the bucket ARN built from the bucket
    *name* rather than ``bucket.bucket_arn`` (a ``GetAtt`` on the bucket). That breaks
    the Bucket -> QueuePolicy -> Bucket cycle when the notification configuration is
    set natively on the bucket.
    """

    def __init__(self, queue: sqs.IQueue, *, source_bucket_arn: str | None = None) -> None:
        self._queue = queue
        self._source_bucket_arn = source_bucket_arn
        self._config: s3.BucketNotificationDestinationConfig | None = None

    def bind(
        self, scope: Construct, bucket: s3_refs.IBucketRef
    ) -> s3.BucketNotificationDestinationConfig:
        # bind() may be called once per notification; add the policy statement only once.
        if self._config is not None:
            return self._config

        source_arn = self._source_bucket_arn or bucket.bucket_ref.bucket_arn
        result = self._queue.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowS3BucketNotificationsFromIngestionBucket",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("s3.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[self._queue.queue_arn],
                conditions={
                    "ArnEquals": {"aws:SourceArn": source_arn},
                    "StringEquals": {"aws:SourceAccount": Stack.of(self._queue).account},
                },
            )
        )
        dependencies: list[IDependable] = []
        if result.policy_dependable is not None:
            dependencies.append(result.policy_dependable)

        self._config = s3.BucketNotificationDestinationConfig(
            arn=self._queue.queue_arn,
            type=s3.BucketNotificationDestinationType.QUEUE,
            dependencies=dependencies,
        )
        return self._config


class ProcessingQueues(Construct):
    """``file-processing-queue`` with redrive to ``file-processing-dlq``.

    Both use SQS-managed encryption on purpose: with a customer-managed KMS key, S3
    would also need ``kms:GenerateDataKey``/``kms:Decrypt`` in the key policy, and a
    missing grant makes notifications fail silently.
    """

    def __init__(self, scope: Construct, construct_id: str, *, worker_timeout: Duration) -> None:
        super().__init__(scope, construct_id)

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetterQueue",
            queue_name="file-processing-dlq",
            retention_period=DLQ_RETENTION,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )

        self.queue = sqs.Queue(
            self,
            "Queue",
            queue_name="file-processing-queue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            # AWS guidance: >= 6 x function timeout, so a message isn't redelivered while
            # the poller is still retrying a throttled or slow invocation.
            visibility_timeout=Duration.seconds(
                worker_timeout.to_seconds() * VISIBILITY_TIMEOUT_MULTIPLIER
            ),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dead_letter_queue,
                max_receive_count=MAX_RECEIVE_COUNT,
            ),
        )

    def notification_destination(self, *, source_bucket_arn: str) -> S3QueueNotificationDestination:
        return S3QueueNotificationDestination(self.queue, source_bucket_arn=source_bucket_arn)

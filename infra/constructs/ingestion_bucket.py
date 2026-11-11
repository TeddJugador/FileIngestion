"""Versioned ingestion bucket: raw uploads in, claim-check files out."""

from __future__ import annotations

from aws_cdk import Acknowledgment, ArnFormat, Duration, RemovalPolicy, Stack, Validations
from aws_cdk import aws_s3 as s3
from constructs import Construct

RAW_PREFIX = "raw/"
PROCESSED_PREFIX = "processed/"

# S3 allows one prefix and one suffix per notification filter, so one notification per
# extension. The worker rejects document extensions under tabular datasets and vice versa.
NOTIFICATION_SUFFIXES: tuple[str, ...] = (".csv", ".json", ".jsonl", ".pdf", ".docx", ".md", ".txt")

# Retention is driven by data protection (raw/ and processed/ hold minors' personal
# data), not by storage cost. processed/ must outlive the 30-day event archive so a
# replayed ImportCompleted/DocumentAccepted still finds its claim-check files.
RAW_EXPIRATION = Duration.days(30)
PROCESSED_EXPIRATION = Duration.days(45)
# On a versioned bucket, "expire after 30 days" only turns the current version into a
# noncurrent one; the bytes are deleted when the noncurrent version expires. 1 day keeps
# real deletion at ~31 days (raw/) and ~46 days (processed/) instead of ~60 and ~75.
NONCURRENT_VERSION_EXPIRATION = Duration.days(1)
INCOMPLETE_MULTIPART_ABORT = Duration.days(1)


def ingestion_bucket_name(scope: Construct) -> str:
    stack = Stack.of(scope)
    return f"school-ingestion-{stack.account}-{stack.region}"


def bucket_arn_for_name(scope: Construct, bucket_name: str) -> str:
    """``arn:<partition>:s3:::<name>`` without referencing the bucket resource."""
    return Stack.of(scope).format_arn(
        service="s3",
        region="",
        account="",
        resource=bucket_name,
        arn_format=ArnFormat.NO_RESOURCE_NAME,
    )


class IngestionBucket(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_name: str,
        notification_destination: s3.IBucketNotificationDestination,
    ) -> None:
        super().__init__(scope, construct_id)

        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # Required: the worker identifies files by versionId, not ETag.
            versioned=True,
            # Teardown: empty the bucket (including noncurrent versions) first; see README.
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                # No Standard-IA (30-day minimum charge) or Glacier (90-day minimum):
                # neither saves anything at these retention periods.
                s3.LifecycleRule(id="expire-raw", prefix=RAW_PREFIX, expiration=RAW_EXPIRATION),
                s3.LifecycleRule(
                    id="expire-processed",
                    prefix=PROCESSED_PREFIX,
                    expiration=PROCESSED_EXPIRATION,
                ),
                s3.LifecycleRule(
                    id="noncurrent-and-multipart-cleanup",
                    noncurrent_version_expiration=NONCURRENT_VERSION_EXPIRATION,
                    abort_incomplete_multipart_upload_after=INCOMPLETE_MULTIPART_ABORT,
                    # Removes the delete markers left once all noncurrent versions are gone.
                    expired_object_delete_marker=True,
                ),
            ],
        )

        self._configure_notifications(notification_destination)

        # Narrowest scope: the CfnBucket only, not its bucket policy.
        Validations.of(self.bucket.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(
                id="AwsSolutions-S1",
                reason=(
                    "Portfolio-scale service: a server-access-log bucket would itself "
                    "need logging. Production alternative: CloudTrail S3 data events "
                    "(see README)."
                ),
            )
        )

    def _configure_notifications(self, destination: s3.IBucketNotificationDestination) -> None:
        """Set the notification configuration natively on AWS::S3::Bucket.

        ``Bucket.add_event_notification`` would deploy CDK's BucketNotificationsHandler
        custom-resource Lambda, whose role uses the AWSLambdaBasicExecutionRole managed
        policy. That breaks the "no AWS managed policies" rule and adds cdk-nag findings
        (IAM4/IAM5/L1) that would need unapproved suppressions. The destination's bind()
        contract (policy statement + dependency) is used exactly as CDK would use it.
        """
        config = destination.bind(self, self.bucket)
        cfn_bucket = self.bucket.node.default_child
        assert isinstance(cfn_bucket, s3.CfnBucket)

        cfn_bucket.notification_configuration = s3.CfnBucket.NotificationConfigurationProperty(
            queue_configurations=[
                s3.CfnBucket.QueueConfigurationProperty(
                    event="s3:ObjectCreated:*",
                    queue=config.arn,
                    filter=s3.CfnBucket.NotificationFilterProperty(
                        s3_key=s3.CfnBucket.S3KeyFilterProperty(
                            rules=[
                                s3.CfnBucket.FilterRuleProperty(name="prefix", value=RAW_PREFIX),
                                s3.CfnBucket.FilterRuleProperty(name="suffix", value=suffix),
                            ]
                        )
                    ),
                )
                for suffix in NOTIFICATION_SUFFIXES
            ]
        )
        for dependency in config.dependencies or []:
            cfn_bucket.node.add_dependency(dependency)

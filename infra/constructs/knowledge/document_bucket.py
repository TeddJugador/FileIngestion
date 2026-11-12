"""The knowledge service's own copy of every indexed document version."""

from __future__ import annotations

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Stack, Validations
from aws_cdk import aws_s3 as s3
from constructs import Construct

SOURCES_PREFIX = "sources/"
VECTOR_KEYS_PREFIX = "vector-keys/"


class KnowledgeDocumentBucket(Construct):
    """``sources/<school>/<doc>/<version>/source.<ext>`` and
    ``vector-keys/<school>/<doc>/<version>.json``.

    Retention follows the document registry, not a clock: a version's files are deleted
    when it is superseded, retired or permanently fails, so there is no age-based expiry
    (an active policy may stay live for years). Versioning is off because every key
    already contains the document version and objects are never overwritten.
    """

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=f"school-knowledge-docs-{stack.account}-{stack.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-multipart",
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )
        Validations.of(self.bucket.node.default_child).acknowledge(  # type: ignore[arg-type]
            Acknowledgment(
                id="AwsSolutions-S1",
                reason="Portfolio-scale service: a server-access-log bucket would itself need "
                "logging. Production alternative: CloudTrail S3 data events (see README).",
            )
        )

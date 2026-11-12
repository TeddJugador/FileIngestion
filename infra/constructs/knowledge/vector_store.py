"""S3 Vectors: one vector bucket and one index bound to the embedding model's dimension."""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_s3vectors as s3vectors
from constructs import Construct

from infra.constructs.knowledge.limits import NON_FILTERABLE_METADATA_KEYS


class VectorStore(Construct):
    def __init__(
        self, scope: Construct, construct_id: str, *, index_name: str, dimension: int
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        self.bucket_name = f"school-knowledge-vectors-{stack.account}-{stack.region}"

        self.vector_bucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket", vector_bucket_name=self.bucket_name
        )
        self.vector_bucket.apply_removal_policy(RemovalPolicy.DESTROY)

        self.index = s3vectors.CfnIndex(
            self,
            "Index",
            vector_bucket_name=self.bucket_name,
            index_name=index_name,
            data_type="float32",
            dimension=dimension,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                # Large fields stay out of the 2 KB filterable budget.
                non_filterable_metadata_keys=list(NON_FILTERABLE_METADATA_KEYS)
            ),
        )
        self.index.node.add_dependency(self.vector_bucket)  # DependsOn: bucket first
        self.index.apply_removal_policy(RemovalPolicy.DESTROY)

        self.index_name = index_name
        self.index_arn = self.index.attr_index_arn

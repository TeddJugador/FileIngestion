"""DynamoDB tables owned by the knowledge service."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

BY_SCHOOL_STATUS_INDEX = "BySchoolStatus"


def _pitr() -> dynamodb.PointInTimeRecoverySpecification:
    return dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)


class KnowledgeTables(Construct):
    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        string = dynamodb.AttributeType.STRING

        # Document items (SK "META") and version items (SK "VER#<version>").
        self.registry = dynamodb.Table(
            self,
            "DocumentRegistry",
            table_name="DocumentRegistry",
            partition_key=dynamodb.Attribute(name="PK", type=string),
            sort_key=dynamodb.Attribute(name="SK", type=string),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=_pitr(),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Admin listing: META items carry school_id and "<status>#<updated_at>".
        self.registry.add_global_secondary_index(
            index_name=BY_SCHOOL_STATUS_INDEX,
            partition_key=dynamodb.Attribute(name="school_id", type=string),
            sort_key=dynamodb.Attribute(name="status_updated_at", type=string),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.idempotency = dynamodb.Table(
            self,
            "KnowledgeIdempotency",
            table_name="KnowledgeIdempotency",
            partition_key=dynamodb.Attribute(name="id", type=string),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiration",
            point_in_time_recovery_specification=_pitr(),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Who asked which question (for the feedback ownership check). No text, 7-day TTL.
        self.query_log = dynamodb.Table(
            self,
            "QueryLog",
            table_name="QueryLog",
            partition_key=dynamodb.Attribute(name="queryId", type=string),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=_pitr(),
            removal_policy=RemovalPolicy.DESTROY,
        )

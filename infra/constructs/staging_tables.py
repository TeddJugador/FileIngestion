"""DynamoDB tables owned by the ingestion service. No other service is granted access."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

BY_IMPORT_INDEX = "ByImport"


class StagingTables(Construct):
    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        # Internal staging table: per-key history across imports (record_hash makes
        # conflicting re-uploads visible). Consumers read valid.jsonl, never this.
        self.records = dynamodb.Table(
            self,
            "IngestedRecords",
            table_name="IngestedRecords",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl_timestamp",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # For this service's own ops tooling and tests: inspect one import.
        self.records.add_global_secondary_index(
            index_name=BY_IMPORT_INDEX,
            partition_key=dynamodb.Attribute(name="import_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Powertools idempotency records (24 h). PITR also satisfies cdk-nag DDB3.
        self.idempotency = dynamodb.Table(
            self,
            "IngestionIdempotency",
            table_name="IngestionIdempotency",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiration",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

"""Knowledge service stack (retrieval-augmented question answering over school documents)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_events as events
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.constructs.dependencies_layer import DependenciesLayer
from infra.constructs.knowledge.admin_ops_function import AdminOpsFunction
from infra.constructs.knowledge.alarms import KnowledgeAlarms
from infra.constructs.knowledge.document_bucket import KnowledgeDocumentBucket
from infra.constructs.knowledge.indexer_function import IndexerFunction
from infra.constructs.knowledge.indexing_queue import IndexingQueue
from infra.constructs.knowledge.query_api import QueryApi
from infra.constructs.knowledge.registry import KnowledgeTables
from infra.constructs.knowledge.vector_store import VectorStore
from infra.knowledge_config import KnowledgeConfig


class KnowledgeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: KnowledgeConfig,
        event_bus: events.IEventBus,
        ops_alerts_topic: sns.ITopic,
        user_pool: cognito.IUserPool,
        user_pool_clients: list[cognito.IUserPoolClient],
        ingestion_bucket: s3.IBucket,
        layer_asset_path: str | Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.event_bus = event_bus
        self.ops_alerts_topic = ops_alerts_topic
        self.user_pool = user_pool
        # Read-only, and only processed/documents/* (granted to the indexer).
        self.ingestion_bucket = ingestion_bucket

        self.documents = KnowledgeDocumentBucket(self, "Documents")
        self.vectors = VectorStore(
            self,
            "Vectors",
            index_name=config.index_name,
            dimension=config.embedding_dimension,
        )
        self.tables = KnowledgeTables(self, "Tables")
        self.indexing = IndexingQueue(self, "Indexing", event_bus=event_bus)
        self.dependencies_layer = DependenciesLayer(
            self,
            "Dependencies",
            asset_path=layer_asset_path,
            description="Knowledge deps (Powertools, pydantic, pypdf, python-docx, httpx)",
        ).layer

        self.indexer = IndexerFunction(
            self,
            "Indexer",
            config=config,
            queue=self.indexing.queue,
            knowledge_bucket=self.documents.bucket,
            vectors=self.vectors,
            registry=self.tables.registry,
            idempotency=self.tables.idempotency,
            event_bus=event_bus,
            layer=self.dependencies_layer,
        )

        self.query_api = QueryApi(
            self,
            "Query",
            config=config,
            user_pool=user_pool,
            user_pool_clients=user_pool_clients,
            knowledge_bucket=self.documents.bucket,
            vectors=self.vectors,
            registry=self.tables.registry,
            query_log=self.tables.query_log,
            event_bus=event_bus,
            layer=self.dependencies_layer,
        )
        self.admin_ops = AdminOpsFunction(
            self,
            "AdminOps",
            registry=self.tables.registry,
            queue=self.indexing.queue,
            dead_letter_queue=self.indexing.dead_letter_queue,
            index_name=config.index_name,
            layer=self.dependencies_layer,
        )
        self.alarms = KnowledgeAlarms(
            self,
            "Alarms",
            config=config,
            dead_letter_queue=self.indexing.dead_letter_queue,
            query_function=self.query_api.function,
            topic=ops_alerts_topic,
        )
        CfnOutput(self, "AdminOpsFunctionName", value=self.admin_ops.function.function_name)
        CfnOutput(self, "ApiUrl", value=self.query_api.url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)

        CfnOutput(self, "KnowledgeBucketName", value=self.documents.bucket.bucket_name)
        CfnOutput(self, "VectorBucketName", value=self.vectors.bucket_name)
        CfnOutput(self, "VectorIndexName", value=config.index_name)
        CfnOutput(
            self, "IndexingDeadLetterQueueUrl", value=self.indexing.dead_letter_queue.queue_url
        )

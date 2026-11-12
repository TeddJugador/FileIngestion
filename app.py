#!/usr/bin/env python3
"""CDK entry point: SharedStack + IngestionStack, with cdk-nag AwsSolutions checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infra.constructs.dependencies_layer import (
    DEFAULT_KNOWLEDGE_LAYER_ASSET_PATH,
    DEFAULT_LAYER_ASSET_PATH,
)
from infra.ingestion_stack import IngestionStack
from infra.knowledge_config import KnowledgeConfig
from infra.knowledge_stack import KnowledgeStack
from infra.shared_stack import SharedStack


@dataclass(frozen=True)
class Stacks:
    shared: SharedStack
    ingestion: IngestionStack
    knowledge: KnowledgeStack


def build_stacks(
    app: cdk.App,
    *,
    layer_asset_path: str | Path = DEFAULT_LAYER_ASSET_PATH,
    knowledge_layer_asset_path: str | Path = DEFAULT_KNOWLEDGE_LAYER_ASSET_PATH,
) -> Stacks:
    # Account/region come from the CLI profile; nothing is hard-coded.
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    )
    # Optional: cdk deploy --all -c alertEmail=you@example.com
    alert_email = app.node.try_get_context("alertEmail")
    knowledge_config = KnowledgeConfig.from_node(app.node)
    shared = SharedStack(
        app,
        "SchoolSharedStack",
        env=env,
        alert_email=alert_email,
        stage=knowledge_config.stage,
    )
    ingestion = IngestionStack(
        app,
        "SchoolIngestionStack",
        env=env,
        event_bus=shared.event_bus,
        ops_alerts_topic=shared.ops_alerts_topic,
        layer_asset_path=layer_asset_path,
    )
    knowledge = KnowledgeStack(
        app,
        "SchoolKnowledgeStack",
        env=env,
        config=knowledge_config,
        event_bus=shared.event_bus,
        ops_alerts_topic=shared.ops_alerts_topic,
        user_pool=shared.user_pool,
        user_pool_clients=[c for c in (shared.web_client, shared.test_client) if c is not None],
        ingestion_bucket=ingestion.storage.bucket,
        layer_asset_path=knowledge_layer_asset_path,
    )
    # The knowledge stack reads the ingestion bucket by name (no cross-stack import),
    # so the ordering is explicit.
    knowledge.add_stack_dependency(ingestion)
    # cdk-nag v3 runs as a CDK policy-validation plugin: any unacknowledged finding
    # fails synthesis. Acknowledgements are also written to template metadata
    # (cdk_nag.rules_to_suppress) so they're auditable and testable.
    cdk.Validations.of(app).add_plugins(
        AwsSolutionsChecks(app, verbose=True, write_suppressions_to_cloud_formation=True)
    )
    return Stacks(shared=shared, ingestion=ingestion, knowledge=knowledge)


if __name__ == "__main__":
    application = cdk.App()
    build_stacks(application)
    application.synth()

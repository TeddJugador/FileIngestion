"""Phase (d) infrastructure: the indexer Lambda, its permissions and its trigger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from app import build_stacks
from infra.constructs.dependencies_layer import PYTHON_VERSION
from tests.infra.conftest import Synthesized, SynthFn, as_list, cdk_context, resources_of_type


def _indexer(template: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (match,) = [
        (k, v)
        for k, v in resources_of_type(template, "AWS::Lambda::Function").items()
        if v["Properties"]["Handler"] == "knowledge.handlers.indexer.handler"
    ]
    return match


def _statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    _, function = _indexer(template)
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return [
        s
        for policy in resources_of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Properties"]["Roles"]
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _by_sid(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["Sid"]: s for s in _statements(template) if "Sid" in s}


def test_indexer_function_settings(synthesized: Synthesized) -> None:
    _, function = _indexer(synthesized.knowledge_json)
    props = function["Properties"]
    assert props["MemorySize"] == 1536
    assert props["Timeout"] == 600
    assert props["Architectures"] == ["arm64"]
    assert props["Runtime"] == f"python{PYTHON_VERSION}"
    assert props["TracingConfig"] == {"Mode": "Active"}


def test_indexer_trigger(synthesized: Synthesized) -> None:
    (mapping,) = resources_of_type(
        synthesized.knowledge_json, "AWS::Lambda::EventSourceMapping"
    ).values()
    props = mapping["Properties"]
    assert props["BatchSize"] == 1
    assert props["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert props["ScalingConfig"] == {"MaximumConcurrency": 2}  # provider rate limits


def test_the_only_grant_on_the_ingestion_bucket_is_reading_staged_documents(
    synthesized: Synthesized,
) -> None:
    ingestion_grants = [
        s
        for s in _statements(synthesized.knowledge_json)
        if "school-ingestion-" in json.dumps(s["Resource"])
    ]
    (grant,) = ingestion_grants
    assert as_list(grant["Action"]) == ["s3:GetObject"]
    assert json.dumps(grant["Resource"]).endswith('/processed/documents/*"]]}')
    # And nothing in any knowledge policy mentions the ingestion stack's bucket export.
    assert "StorageBucket" not in json.dumps(synthesized.knowledge_json)


def test_indexer_permissions(synthesized: Synthesized) -> None:
    sids = _by_sid(synthesized.knowledge_json)
    assert set(as_list(sids["OwnSourcesAndKeyLists"]["Action"])) == {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    }
    resources = json.dumps(sids["OwnSourcesAndKeyLists"]["Resource"])
    assert "/sources/*" in resources
    assert "/vector-keys/*" in resources
    assert set(as_list(sids["WriteVectors"]["Action"])) == {
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:DeleteVectors",
    }
    assert "IndexArn" in json.dumps(sids["WriteVectors"]["Resource"])  # the one index
    assert set(as_list(sids["DocumentRegistry"]["Action"])) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    }
    assert as_list(sids["EmbeddingProviderKey"]["Action"]) == ["secretsmanager:GetSecretValue"]
    assert ":secret:knowledge/gemini-??????" in json.dumps(sids["EmbeddingProviderKey"]["Resource"])
    assert as_list(sids["PublishEvents"]["Action"]) == ["events:PutEvents"]


def test_no_s3vectors_wildcard_actions(synthesized: Synthesized) -> None:
    for statement in _statements(synthesized.knowledge_json):
        for action in as_list(statement["Action"]):
            assert not action.endswith(":*"), action


def test_secret_follows_the_configured_embedding_provider(synth: SynthFn) -> None:
    template = synth(embeddingProvider="voyage", embeddingDimension=1024)["knowledge"]
    assert ":secret:knowledge/voyage-??????" in json.dumps(
        _by_sid(template)["EmbeddingProviderKey"]
    )


def test_environment_has_configuration_but_no_secrets(synthesized: Synthesized) -> None:
    _, function = _indexer(synthesized.knowledge_json)
    env = function["Properties"]["Environment"]["Variables"]
    assert env["EMBEDDING_PROVIDER"] == "gemini"
    assert env["EMBEDDING_MODEL"] == "gemini-embedding-2"
    assert env["EMBEDDING_DIMENSION"] == "768"
    assert env["VECTOR_INDEX_NAME"] == "docs-gemini-768-v1"
    assert env["VECTOR_PUT_BATCH_SIZE"] == "500"
    assert env["MAX_RECEIVE_COUNT"] == "3"
    for name, value in env.items():
        text = json.dumps(value).lower()
        assert "key" not in name.lower() or name in ("VECTOR_KEYS_PREFIX",), name
        assert "secret" not in text
        assert "apikey" not in text


def test_knowledge_stack_deploys_after_ingestion(fake_layer_dir: Path) -> None:
    """The dependency is explicit, since the knowledge stack imports nothing from it."""
    app = cdk.App(context=cdk_context())
    stacks = build_stacks(
        app, layer_asset_path=fake_layer_dir, knowledge_layer_asset_path=fake_layer_dir
    )
    assert stacks.ingestion in stacks.knowledge.dependencies


def test_synth_passes_cdk_nag_with_a_real_account_and_region(
    fake_layer_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cdk synth` from the CLI knows the account and region, which changes how ARNs
    render (and so the IDs cdk-nag findings are acknowledged by). The other infra tests
    synthesize environment-agnostic; this one covers the CLI's case."""
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
    monkeypatch.setenv("CDK_DEFAULT_REGION", "af-south-1")
    app = cdk.App(context=cdk_context())
    build_stacks(app, layer_asset_path=fake_layer_dir, knowledge_layer_asset_path=fake_layer_dir)
    app.synth()  # raises on any unacknowledged finding

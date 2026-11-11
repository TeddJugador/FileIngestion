from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from infra.constructs.dependencies_layer import PYTHON_VERSION, DependenciesLayer
from infra.constructs.worker_function import WORKER_TIMEOUT
from tests.infra.conftest import Synthesized, as_list, resources_of_type

EXPECTED_SUFFIXES = {".csv", ".json", ".jsonl", ".pdf", ".docx", ".md", ".txt"}


def _bucket(s: Synthesized) -> tuple[str, dict[str, Any]]:
    buckets = resources_of_type(s.ingestion_json, "AWS::S3::Bucket")
    assert len(buckets) == 1
    return next(iter(buckets.items()))


def _queue(s: Synthesized, name: str) -> tuple[str, dict[str, Any]]:
    matches = {
        k: v
        for k, v in resources_of_type(s.ingestion_json, "AWS::SQS::Queue").items()
        if v["Properties"]["QueueName"] == name
    }
    assert len(matches) == 1, name
    return next(iter(matches.items()))


def _policy_for_queue(s: Synthesized, queue_logical_id: str) -> dict[str, Any]:
    policies = [
        p
        for p in resources_of_type(s.ingestion_json, "AWS::SQS::QueuePolicy").values()
        if {"Ref": queue_logical_id} in p["Properties"]["Queues"]
    ]
    assert len(policies) == 1
    return policies[0]


# --- bucket -----------------------------------------------------------------------


def test_bucket_name_is_built_from_account_and_region_tokens(synthesized: Synthesized) -> None:
    _, bucket = _bucket(synthesized)
    assert bucket["Properties"]["BucketName"] == {
        "Fn::Join": [
            "",
            ["school-ingestion-", {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}],
        ]
    }


def test_bucket_is_versioned_encrypted_and_private(synthesized: Synthesized) -> None:
    _, bucket = _bucket(synthesized)
    props = bucket["Properties"]
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    ]
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_bucket_policy_enforces_ssl(synthesized: Synthesized) -> None:
    (policy,) = resources_of_type(synthesized.ingestion_json, "AWS::S3::BucketPolicy").values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )


def test_lifecycle_rules(synthesized: Synthesized) -> None:
    _, bucket = _bucket(synthesized)
    rules = {r["Id"]: r for r in bucket["Properties"]["LifecycleConfiguration"]["Rules"]}

    raw = rules["expire-raw"]
    assert raw["Prefix"] == "raw/"
    assert raw["ExpirationInDays"] == 30

    processed = rules["expire-processed"]
    assert processed["Prefix"] == "processed/"
    assert processed["ExpirationInDays"] == 45

    cleanup = rules["noncurrent-and-multipart-cleanup"]
    assert cleanup["NoncurrentVersionExpiration"] == {"NoncurrentDays": 1}
    assert cleanup["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}

    for rule in rules.values():
        assert rule["Status"] == "Enabled"
        # No Standard-IA or Glacier transitions anywhere.
        assert "Transitions" not in rule
        assert "Transition" not in rule
        assert "NoncurrentVersionTransitions" not in rule


def test_processed_outlives_the_event_archive(synthesized: Synthesized) -> None:
    _, bucket = _bucket(synthesized)
    rules = {r["Id"]: r for r in bucket["Properties"]["LifecycleConfiguration"]["Rules"]}
    (archive,) = resources_of_type(synthesized.shared_json, "AWS::Events::Archive").values()
    assert rules["expire-processed"]["ExpirationInDays"] > archive["Properties"]["RetentionDays"]


def test_seven_notifications_with_raw_prefix_and_one_suffix_each(
    synthesized: Synthesized,
) -> None:
    _, bucket = _bucket(synthesized)
    main_queue_id, _ = _queue(synthesized, "file-processing-queue")
    configs = bucket["Properties"]["NotificationConfiguration"]["QueueConfigurations"]
    assert len(configs) == 7

    suffixes = set()
    for config in configs:
        assert config["Event"] == "s3:ObjectCreated:*"
        assert config["Queue"] == {"Fn::GetAtt": [main_queue_id, "Arn"]}
        rules = {r["Name"]: r["Value"] for r in config["Filter"]["S3Key"]["Rules"]}
        assert rules["prefix"] == "raw/"
        suffixes.add(rules["suffix"])
    assert suffixes == EXPECTED_SUFFIXES

    for kind in ("Topic", "Lambda", "EventBridge"):
        assert f"{kind}Configurations" not in bucket["Properties"]["NotificationConfiguration"]


def test_notifications_need_no_custom_resource_lambda(synthesized: Synthesized) -> None:
    for template in (synthesized.ingestion_json, synthesized.shared_json):
        assert not resources_of_type(template, "Custom::S3BucketNotifications")
        handlers = [
            k
            for k in resources_of_type(template, "AWS::Lambda::Function")
            if k.startswith("BucketNotificationsHandler")
        ]
        assert handlers == []


def test_bucket_is_created_after_the_queue_policy(synthesized: Synthesized) -> None:
    _, bucket = _bucket(synthesized)
    main_queue_id, _ = _queue(synthesized, "file-processing-queue")
    policy_ids = [
        k
        for k, v in resources_of_type(synthesized.ingestion_json, "AWS::SQS::QueuePolicy").items()
        if {"Ref": main_queue_id} in v["Properties"]["Queues"]
    ]
    assert len(policy_ids) == 1
    assert policy_ids[0] in as_list(bucket.get("DependsOn", []))


# --- queues -----------------------------------------------------------------------


def test_visibility_timeout_is_six_times_worker_timeout(synthesized: Synthesized) -> None:
    _, queue = _queue(synthesized, "file-processing-queue")
    assert WORKER_TIMEOUT.to_seconds() == 300
    assert queue["Properties"]["VisibilityTimeout"] == 6 * WORKER_TIMEOUT.to_seconds() == 1800


def test_redrive_to_dlq_after_three_receives(synthesized: Synthesized) -> None:
    dlq_id, _ = _queue(synthesized, "file-processing-dlq")
    _, queue = _queue(synthesized, "file-processing-queue")
    assert queue["Properties"]["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": [dlq_id, "Arn"]},
        "maxReceiveCount": 3,
    }


def test_dlq_retention_is_14_days(synthesized: Synthesized) -> None:
    _, dlq = _queue(synthesized, "file-processing-dlq")
    assert dlq["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


@pytest.mark.parametrize("name", ["file-processing-queue", "file-processing-dlq"])
def test_queues_use_sqs_managed_encryption_and_enforce_ssl(
    synthesized: Synthesized, name: str
) -> None:
    queue_id, queue = _queue(synthesized, name)
    assert queue["Properties"]["SqsManagedSseEnabled"] is True
    assert "KmsMasterKeyId" not in queue["Properties"]
    statements = _policy_for_queue(synthesized, queue_id)["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )


def test_queue_policy_has_one_s3_statement_with_both_conditions(
    synthesized: Synthesized,
) -> None:
    queue_id, _ = _queue(synthesized, "file-processing-queue")
    statements = _policy_for_queue(synthesized, queue_id)["Properties"]["PolicyDocument"][
        "Statement"
    ]
    s3_statements = [
        s for s in statements if s.get("Principal", {}).get("Service") == "s3.amazonaws.com"
    ]
    assert len(s3_statements) == 1
    (statement,) = s3_statements

    assert statement["Effect"] == "Allow"
    assert as_list(statement["Action"]) == ["sqs:SendMessage"]
    assert statement["Resource"] == {"Fn::GetAtt": [queue_id, "Arn"]}
    assert statement["Condition"]["StringEquals"] == {
        "aws:SourceAccount": {"Ref": "AWS::AccountId"}
    }

    # SourceArn is the ingestion bucket's ARN, built from its name.
    _, bucket = _bucket(synthesized)
    name_parts = bucket["Properties"]["BucketName"]["Fn::Join"][1]
    assert statement["Condition"]["ArnEquals"]["aws:SourceArn"] == {
        "Fn::Join": [
            "",
            ["arn:", {"Ref": "AWS::Partition"}, ":s3:::" + name_parts[0], *name_parts[1:]],
        ]
    }


def test_dlq_policy_grants_nothing_to_s3(synthesized: Synthesized) -> None:
    dlq_id, _ = _queue(synthesized, "file-processing-dlq")
    statements = _policy_for_queue(synthesized, dlq_id)["Properties"]["PolicyDocument"]["Statement"]
    assert all(s["Effect"] == "Deny" for s in statements)


# --- dependencies layer -------------------------------------------------------------


def test_layer_targets_arm64_and_the_pinned_python_version(synthesized: Synthesized) -> None:
    (layer,) = resources_of_type(synthesized.ingestion_json, "AWS::Lambda::LayerVersion").values()
    assert layer["Properties"]["CompatibleRuntimes"] == [f"python{PYTHON_VERSION}"]
    assert layer["Properties"]["CompatibleArchitectures"] == ["arm64"]


def test_runtime_is_3_13_or_later() -> None:
    major, minor = (int(p) for p in PYTHON_VERSION.split("."))
    assert (major, minor) >= (3, 13)


def test_build_script_reads_the_same_version_file() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "build_layer.sh").read_text()
    assert ".lambda-python-version" in script
    assert "manylinux2014_aarch64" in script
    assert "--only-binary=:all:" in script


def test_missing_layer_build_fails_with_a_clear_message(tmp_path: Path) -> None:
    stack = cdk.Stack(cdk.App(), "T")
    with pytest.raises(FileNotFoundError, match=r"build_layer\.sh"):
        DependenciesLayer(stack, "L", asset_path=tmp_path / "missing")


def test_template_is_json_serialisable(synthesized: Synthesized) -> None:
    json.dumps(synthesized.ingestion_json)

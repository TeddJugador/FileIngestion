"""KnowledgeStack phase (a): buckets, vector index, tables, indexing queue and rule."""

from __future__ import annotations

from typing import Any

import pytest

from infra.constructs.dependencies_layer import PYTHON_VERSION
from tests.infra.conftest import Synthesized, SynthFn, resources_of_type


def _one(template: dict[str, Any], resource_type: str, **props: Any) -> tuple[str, dict[str, Any]]:
    matches = {
        k: v
        for k, v in resources_of_type(template, resource_type).items()
        if all(v["Properties"].get(p) == val for p, val in props.items())
    }
    assert len(matches) == 1, (resource_type, props, list(matches))
    return next(iter(matches.items()))


def _queue(s: Synthesized, name: str) -> tuple[str, dict[str, Any]]:
    return _one(s.knowledge_json, "AWS::SQS::Queue", QueueName=name)


# --- knowledge document bucket -------------------------------------------------------


def _docs_bucket(s: Synthesized) -> tuple[str, dict[str, Any]]:
    (item,) = resources_of_type(s.knowledge_json, "AWS::S3::Bucket").items()
    return item


def test_knowledge_bucket_name_from_tokens(synthesized: Synthesized) -> None:
    _, bucket = _docs_bucket(synthesized)
    assert bucket["Properties"]["BucketName"] == {
        "Fn::Join": [
            "",
            ["school-knowledge-docs-", {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}],
        ]
    }


def test_knowledge_bucket_is_encrypted_private_and_unversioned(synthesized: Synthesized) -> None:
    _, bucket = _docs_bucket(synthesized)
    props = bucket["Properties"]
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    ]
    assert all(props["PublicAccessBlockConfiguration"].values())
    assert "VersioningConfiguration" not in props


def test_knowledge_bucket_enforces_ssl(synthesized: Synthesized) -> None:
    (policy,) = resources_of_type(synthesized.knowledge_json, "AWS::S3::BucketPolicy").values()
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    )


def test_knowledge_bucket_lifecycle_has_no_age_based_expiry(synthesized: Synthesized) -> None:
    _, bucket = _docs_bucket(synthesized)
    (rule,) = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}
    for key in ("ExpirationInDays", "ExpirationDate", "Transitions", "NoncurrentVersionExpiration"):
        assert key not in rule


# --- S3 Vectors ----------------------------------------------------------------------


def test_vector_bucket_name_from_tokens(synthesized: Synthesized) -> None:
    (bucket,) = resources_of_type(
        synthesized.knowledge_json, "AWS::S3Vectors::VectorBucket"
    ).values()
    assert bucket["Properties"]["VectorBucketName"] == {
        "Fn::Join": [
            "",
            ["school-knowledge-vectors-", {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}],
        ]
    }


def test_vector_index_from_default_config(synthesized: Synthesized) -> None:
    (index,) = resources_of_type(synthesized.knowledge_json, "AWS::S3Vectors::Index").values()
    props = index["Properties"]
    assert props["IndexName"] == "docs-gemini-768-v1"
    assert props["Dimension"] == 768
    assert props["DataType"] == "float32"
    assert props["DistanceMetric"] == "cosine"
    assert sorted(props["MetadataConfiguration"]["NonFilterableMetadataKeys"]) == sorted(
        ["chunk_text", "title", "page_start", "page_end", "heading_path"]
    )
    assert "VectorBucketName" in props
    assert "DependsOn" in index


@pytest.mark.parametrize(
    ("context", "name", "dimension"),
    [
        ({"embeddingProvider": "voyage", "embeddingDimension": 1024}, "docs-voyage-1024-v1", 1024),
        ({"vectorIndexVersion": 2}, "docs-gemini-768-v2", 768),
    ],
)
def test_vector_index_follows_config(
    synth: SynthFn, context: dict[str, Any], name: str, dimension: int
) -> None:
    (index,) = resources_of_type(synth(**context)["knowledge"], "AWS::S3Vectors::Index").values()
    assert index["Properties"]["IndexName"] == name
    assert index["Properties"]["Dimension"] == dimension


def test_vector_resources_are_removed_with_the_stack(synthesized: Synthesized) -> None:
    for resource_type in ("AWS::S3Vectors::VectorBucket", "AWS::S3Vectors::Index"):
        for resource in resources_of_type(synthesized.knowledge_json, resource_type).values():
            assert resource["DeletionPolicy"] == "Delete"


# --- tables -----------------------------------------------------------------------------


def test_document_registry(synthesized: Synthesized) -> None:
    _, table = _one(
        synthesized.knowledge_json, "AWS::DynamoDB::Table", TableName="DocumentRegistry"
    )
    props = table["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    (gsi,) = props["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == "BySchoolStatus"
    assert gsi["KeySchema"] == [
        {"AttributeName": "school_id", "KeyType": "HASH"},
        {"AttributeName": "status_updated_at", "KeyType": "RANGE"},
    ]


@pytest.mark.parametrize(
    ("name", "key", "ttl"),
    [("KnowledgeIdempotency", "id", "expiration"), ("QueryLog", "queryId", "expires_at")],
)
def test_ttl_tables(synthesized: Synthesized, name: str, key: str, ttl: str) -> None:
    _, table = _one(synthesized.knowledge_json, "AWS::DynamoDB::Table", TableName=name)
    props = table["Properties"]
    assert props["KeySchema"] == [{"AttributeName": key, "KeyType": "HASH"}]
    assert props["TimeToLiveSpecification"] == {"AttributeName": ttl, "Enabled": True}


def test_every_knowledge_table_has_pitr_and_on_demand(synthesized: Synthesized) -> None:
    tables = resources_of_type(synthesized.knowledge_json, "AWS::DynamoDB::Table").values()
    assert len(tables) == 3
    for table in tables:
        props = table["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


# --- indexing queue and rule -------------------------------------------------------------


def test_indexing_queue_timeouts_and_dlq(synthesized: Synthesized) -> None:
    dlq_id, dlq = _queue(synthesized, "document-indexing-dlq")
    _, queue = _queue(synthesized, "document-indexing-queue")
    assert queue["Properties"]["VisibilityTimeout"] == 3600  # 6 x the 600 s indexer timeout
    assert queue["Properties"]["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": [dlq_id, "Arn"]},
        "maxReceiveCount": 3,
    }
    assert dlq["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 3600


@pytest.mark.parametrize("name", ["document-indexing-queue", "document-indexing-dlq"])
def test_indexing_queues_encrypted_and_ssl_only(synthesized: Synthesized, name: str) -> None:
    queue_id, queue = _queue(synthesized, name)
    assert queue["Properties"]["SqsManagedSseEnabled"] is True
    policies = [
        p
        for p in resources_of_type(synthesized.knowledge_json, "AWS::SQS::QueuePolicy").values()
        if {"Ref": queue_id} in p["Properties"]["Queues"]
    ]
    statements = [s for p in policies for s in p["Properties"]["PolicyDocument"]["Statement"]]
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )


def test_document_accepted_rule(synthesized: Synthesized) -> None:
    (rule,) = resources_of_type(synthesized.knowledge_json, "AWS::Events::Rule").values()
    props = rule["Properties"]
    assert props["EventPattern"] == {
        "source": ["school.ingestion"],
        "detail-type": ["DocumentAccepted"],
    }
    assert "Fn::ImportValue" in props["EventBusName"]  # SchoolEventBus, from SharedStack
    queue_id, _ = _queue(synthesized, "document-indexing-queue")
    dlq_id, _ = _queue(synthesized, "document-indexing-dlq")
    (target,) = props["Targets"]
    assert target["Arn"] == {"Fn::GetAtt": [queue_id, "Arn"]}
    assert target["DeadLetterConfig"] == {"Arn": {"Fn::GetAtt": [dlq_id, "Arn"]}}
    assert target["RetryPolicy"]["MaximumEventAgeInSeconds"] == 86400


def test_only_the_rule_may_send_to_the_indexing_queue(synthesized: Synthesized) -> None:
    queue_id, _ = _queue(synthesized, "document-indexing-queue")
    (rule_id,) = resources_of_type(synthesized.knowledge_json, "AWS::Events::Rule")
    allows = [
        s
        for p in resources_of_type(synthesized.knowledge_json, "AWS::SQS::QueuePolicy").values()
        if {"Ref": queue_id} in p["Properties"]["Queues"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s["Effect"] == "Allow"
    ]
    (allow,) = allows
    assert allow["Principal"] == {"Service": "events.amazonaws.com"}
    assert allow["Condition"]["ArnEquals"]["aws:SourceArn"] == {"Fn::GetAtt": [rule_id, "Arn"]}


# --- layer and boundaries -------------------------------------------------------------


def test_knowledge_layer_is_arm64_on_the_shared_runtime(synthesized: Synthesized) -> None:
    (layer,) = resources_of_type(synthesized.knowledge_json, "AWS::Lambda::LayerVersion").values()
    assert layer["Properties"]["CompatibleRuntimes"] == [f"python{PYTHON_VERSION}"]
    assert layer["Properties"]["CompatibleArchitectures"] == ["arm64"]
    assert "Knowledge deps" in layer["Properties"]["Description"]


def test_no_grants_on_the_ingestion_bucket_yet(synthesized: Synthesized) -> None:
    # Phase (a) creates no functions; later phases grant processed/documents/* only.
    assert resources_of_type(synthesized.knowledge_json, "AWS::IAM::Policy") == {}
    assert resources_of_type(synthesized.knowledge_json, "AWS::Lambda::Function") == {}

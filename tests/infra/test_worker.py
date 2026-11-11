"""Phase (c) infrastructure: staging tables, worker function, role and trigger."""

from __future__ import annotations

import json
from typing import Any

import pytest

from infra.constructs.dependencies_layer import PYTHON_VERSION
from tests.infra.conftest import Synthesized, as_list, resources_of_type


def _one(template: dict[str, Any], resource_type: str, **props: Any) -> tuple[str, dict[str, Any]]:
    matches = {
        k: v
        for k, v in resources_of_type(template, resource_type).items()
        if all(v["Properties"].get(p) == val for p, val in props.items())
    }
    assert len(matches) == 1, (resource_type, props, list(matches))
    return next(iter(matches.items()))


def _worker(s: Synthesized) -> tuple[str, dict[str, Any]]:
    return _one(
        s.ingestion_json, "AWS::Lambda::Function", Handler="ingestion.handlers.etl_worker.handler"
    )


def _role_statements(s: Synthesized, role_id: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for policy in resources_of_type(s.ingestion_json, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in policy["Properties"]["Roles"]:
            statements += policy["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _worker_role_id(s: Synthesized) -> str:
    _, worker = _worker(s)
    return str(worker["Properties"]["Role"]["Fn::GetAtt"][0])


def _actions_on(statements: list[dict[str, Any]], resource_fragment: str) -> set[str]:
    actions: set[str] = set()
    for statement in statements:
        if resource_fragment in json.dumps(statement["Resource"]):
            actions |= set(as_list(statement["Action"]))
    return actions


# --- tables ----------------------------------------------------------------------------


def test_ingested_records_table(synthesized: Synthesized) -> None:
    _, table = _one(synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestedRecords")
    props = table["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl_timestamp", "Enabled": True}
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    (gsi,) = props["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == "ByImport"
    assert gsi["KeySchema"] == [
        {"AttributeName": "import_id", "KeyType": "HASH"},
        {"AttributeName": "PK", "KeyType": "RANGE"},
    ]


def test_idempotency_table(synthesized: Synthesized) -> None:
    _, table = _one(
        synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestionIdempotency"
    )
    props = table["Properties"]
    assert props["KeySchema"] == [{"AttributeName": "id", "KeyType": "HASH"}]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "expiration", "Enabled": True}
    assert props["BillingMode"] == "PAY_PER_REQUEST"


# --- function ----------------------------------------------------------------------------


def test_worker_function_settings(synthesized: Synthesized) -> None:
    _, worker = _worker(synthesized)
    props = worker["Properties"]
    assert props["Runtime"] == f"python{PYTHON_VERSION}"
    assert props["Architectures"] == ["arm64"]
    assert props["MemorySize"] == 1024
    assert props["Timeout"] == 300
    assert props["TracingConfig"] == {"Mode": "Active"}
    assert len(props["Layers"]) == 1
    assert "ReservedConcurrentExecutions" not in props  # backpressure is on the ESM


def test_worker_environment(synthesized: Synthesized) -> None:
    _, worker = _worker(synthesized)
    env = worker["Properties"]["Environment"]["Variables"]
    assert set(env) == {
        "RECORDS_TABLE_NAME",
        "IDEMPOTENCY_TABLE_NAME",
        "EVENT_BUS_NAME",
        "BUCKET_NAME",
        "MAX_TABULAR_BYTES",
        "MAX_DOCUMENT_BYTES",
        "POWERTOOLS_SERVICE_NAME",
        "POWERTOOLS_METRICS_NAMESPACE",
    }
    assert env["MAX_TABULAR_BYTES"] == "20971520"
    assert env["MAX_DOCUMENT_BYTES"] == "26214400"
    assert env["POWERTOOLS_SERVICE_NAME"] == "ingestion"
    assert env["POWERTOOLS_METRICS_NAMESPACE"] == "SchoolSystem/Ingestion"


def test_event_source_mapping(synthesized: Synthesized) -> None:
    (mapping,) = resources_of_type(
        synthesized.ingestion_json, "AWS::Lambda::EventSourceMapping"
    ).values()
    props = mapping["Properties"]
    assert props["BatchSize"] == 1
    assert props["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert props["ScalingConfig"] == {"MaximumConcurrency": 5}


def test_worker_logs_go_to_its_own_log_group(synthesized: Synthesized) -> None:
    (group_id, group) = next(
        iter(resources_of_type(synthesized.ingestion_json, "AWS::Logs::LogGroup").items())
    )
    assert group["Properties"]["RetentionInDays"] == 30
    statements = _role_statements(synthesized, _worker_role_id(synthesized))
    log_statements = [s for s in statements if "logs:PutLogEvents" in as_list(s["Action"])]
    assert len(log_statements) == 1
    assert log_statements[0]["Resource"] == {"Fn::GetAtt": [group_id, "Arn"]}
    assert "logs:CreateLogGroup" not in as_list(log_statements[0]["Action"])


# --- IAM ---------------------------------------------------------------------------------


def test_worker_s3_permissions_are_prefix_scoped(synthesized: Synthesized) -> None:
    statements = _role_statements(synthesized, _worker_role_id(synthesized))
    assert _actions_on(statements, "/raw/*") == {"s3:GetObject", "s3:GetObjectVersion"}
    assert _actions_on(statements, "/processed/*") == {"s3:PutObject", "s3:AbortMultipartUpload"}
    for statement in statements:
        for action in as_list(statement["Action"]):
            if action.startswith("s3:"):
                resource = json.dumps(statement["Resource"])
                assert "/raw/*" in resource or "/processed/*" in resource, statement


def test_worker_dynamodb_and_events_permissions(synthesized: Synthesized) -> None:
    records_id, _ = _one(
        synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestedRecords"
    )
    idem_id, _ = _one(
        synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestionIdempotency"
    )
    statements = _role_statements(synthesized, _worker_role_id(synthesized))
    assert _actions_on(statements, records_id) == {"dynamodb:BatchWriteItem"}
    assert _actions_on(statements, idem_id) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    }
    assert _actions_on(statements, "SchoolEventBus") | _actions_on(statements, "ImportValue") >= {
        "events:PutEvents"
    }


def test_worker_star_resource_is_xray_only(synthesized: Synthesized) -> None:
    statements = _role_statements(synthesized, _worker_role_id(synthesized))
    star = [s for s in statements if s["Resource"] == "*"]
    assert star
    for statement in star:
        assert set(as_list(statement["Action"])) <= {
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
        }


def test_no_other_principal_can_touch_ingested_records(synthesized: Synthesized) -> None:
    records_id, _ = _one(
        synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestedRecords"
    )
    worker_role = _worker_role_id(synthesized)
    for template in (synthesized.ingestion_json, synthesized.shared_json):
        for policy in resources_of_type(template, "AWS::IAM::Policy").values():
            if records_id in json.dumps(policy["Properties"]["PolicyDocument"]):
                assert policy["Properties"]["Roles"] == [{"Ref": worker_role}]
        for role in resources_of_type(template, "AWS::IAM::Role").values():
            assert records_id not in json.dumps(role["Properties"].get("Policies", []))
    _, table = _one(synthesized.ingestion_json, "AWS::DynamoDB::Table", TableName="IngestedRecords")
    assert "ResourcePolicy" not in table["Properties"]


@pytest.mark.parametrize("which", ["ingestion_json"])
def test_iam5_is_acknowledged_per_finding_not_per_rule(
    synthesized: Synthesized, which: str
) -> None:
    template: dict[str, Any] = getattr(synthesized, which)
    ids = [
        rule["id"]
        for r in template["Resources"].values()
        for rule in r.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        if rule["id"].startswith("AwsSolutions-IAM5")
    ]
    assert len(ids) == 3
    assert all("[Resource::" in i for i in ids)
    assert sorted(i.split("[", 1)[1] for i in ids) == sorted(
        [
            "Resource::*]",
            next(i for i in ids if "/processed/*" in i).split("[", 1)[1],
            next(i for i in ids if "/raw/*" in i).split("[", 1)[1],
        ]
    )

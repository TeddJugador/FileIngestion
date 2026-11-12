"""Phase (f) infrastructure: admin_ops and the knowledge alarms."""

from __future__ import annotations

import json
from typing import Any

from tests.infra.conftest import Synthesized, SynthFn, as_list, resources_of_type


def _alarms(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        a["Properties"]["AlarmName"]: a["Properties"]
        for a in resources_of_type(template, "AWS::CloudWatch::Alarm").values()
    }


def _admin(template: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    (function,) = [
        f
        for f in resources_of_type(template, "AWS::Lambda::Function").values()
        if f["Properties"]["Handler"] == "knowledge.handlers.admin_ops.handler"
    ]
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = {
        s["Sid"]: s
        for p in resources_of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "Sid" in s
    }
    return function, statements


def test_four_alarms_notify_the_ops_topic(synthesized: Synthesized) -> None:
    alarms = _alarms(synthesized.knowledge_json)
    assert set(alarms) == {
        "knowledge-indexing-dlq-not-empty",
        "knowledge-query-latency-p95",
        "knowledge-provider-errors",
        "knowledge-embedding-model-mismatch",
    }
    for props in alarms.values():
        assert props["AlarmActions"] == props["OKActions"]
        assert "ImportValue" in json.dumps(props["AlarmActions"])  # the shared topic
        assert props["TreatMissingData"] == "notBreaching"


def test_alarm_thresholds(synthesized: Synthesized) -> None:
    alarms = _alarms(synthesized.knowledge_json)
    latency = alarms["knowledge-query-latency-p95"]
    assert (latency["ExtendedStatistic"], latency["Period"], latency["Threshold"]) == (
        "p95",
        900,
        15000,
    )
    assert alarms["knowledge-indexing-dlq-not-empty"]["Threshold"] == 0
    assert alarms["knowledge-embedding-model-mismatch"]["Threshold"] == 0
    mismatch = alarms["knowledge-embedding-model-mismatch"]
    assert mismatch["Namespace"] == "SchoolSystem/Knowledge"
    assert mismatch["Dimensions"] == [{"Name": "service", "Value": "knowledge-api"}]


def test_provider_alarm_sums_every_provider_in_use(synthesized: Synthesized) -> None:
    alarm = _alarms(synthesized.knowledge_json)["knowledge-provider-errors"]
    assert alarm["Threshold"] == 5
    queries = alarm["Metrics"]
    (expression,) = [q for q in queries if "Expression" in q]
    assert expression["ReturnData"] is True
    dimension_sets = [
        {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}
        for q in queries
        if "MetricStat" in q
    ]
    # Default config: Gemini embeddings (indexer + API) and Claude generation (API).
    assert {(d["service"], d["Provider"]) for d in dimension_sets} == {
        ("knowledge-indexer", "gemini"),
        ("knowledge-api", "gemini"),
        ("knowledge-api", "claude"),
    }
    assert expression["Expression"].count("FILL(") == 3


def test_provider_alarm_follows_the_config(synth: SynthFn) -> None:
    template = synth(generationProvider="GEMINI", generationModel="gemini-3.5-flash-lite")[
        "knowledge"
    ]
    queries = _alarms(template)["knowledge-provider-errors"]["Metrics"]
    providers = {
        d["Value"]
        for q in queries
        if "MetricStat" in q
        for d in q["MetricStat"]["Metric"]["Dimensions"]
        if d["Name"] == "Provider"
    }
    assert providers == {"gemini"}


def test_functions_emit_with_the_service_names_the_alarms_use(synthesized: Synthesized) -> None:
    services = {
        f["Properties"]["Handler"]: f["Properties"]["Environment"]["Variables"].get(
            "POWERTOOLS_SERVICE_NAME"
        )
        for f in resources_of_type(synthesized.knowledge_json, "AWS::Lambda::Function").values()
        if f["Properties"]["Handler"].startswith("knowledge.")
    }
    assert services["knowledge.handlers.indexer.handler"] == "knowledge-indexer"
    assert services["knowledge.handlers.api.handler"] == "knowledge-api"


def test_admin_ops_permissions(synthesized: Synthesized) -> None:
    function, sids = _admin(synthesized.knowledge_json)
    assert function["Properties"]["Timeout"] == 900
    assert set(as_list(sids["FindActiveDocuments"]["Action"])) == {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
    }
    assert "index/*" not in json.dumps(sids["FindActiveDocuments"]["Resource"])
    assert as_list(sids["QueueReindexMessages"]["Action"]) == ["sqs:SendMessage"]
    assert "sqs:DeleteMessage" in as_list(sids["InspectAndRedriveDlq"]["Action"])
    everything = json.dumps(sids)
    for forbidden in ("s3vectors", "secretsmanager", "events:PutEvents", "s3:"):
        assert forbidden not in everything  # re-index is only messages, no data access
    assert set(sids) == {
        "WriteOwnLogs",
        "FindActiveDocuments",
        "QueueReindexMessages",
        "InspectAndRedriveDlq",
    }


def test_admin_ops_targets_the_configured_index(synthesized: Synthesized) -> None:
    function, _ = _admin(synthesized.knowledge_json)
    assert (
        function["Properties"]["Environment"]["Variables"]["VECTOR_INDEX_NAME"]
        == "docs-gemini-768-v1"
    )
    assert "AdminOpsFunctionName" in synthesized.knowledge_json["Outputs"]

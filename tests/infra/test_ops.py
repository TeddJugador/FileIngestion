"""Phase (d) infrastructure: alarms, the DLQ replay function and alert delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from app import build_stacks
from tests.infra.conftest import Synthesized, as_list, resources_of_type


def _alarm(s: Synthesized, name: str) -> dict[str, Any]:
    (alarm,) = [
        a["Properties"]
        for a in resources_of_type(s.ingestion_json, "AWS::CloudWatch::Alarm").values()
        if a["Properties"]["AlarmName"] == name
    ]
    return alarm


def _queue_id(s: Synthesized, name: str) -> str:
    (queue_id,) = [
        k
        for k, v in resources_of_type(s.ingestion_json, "AWS::SQS::Queue").items()
        if v["Properties"]["QueueName"] == name
    ]
    return queue_id


def _function(s: Synthesized, handler: str) -> tuple[str, dict[str, Any]]:
    (match,) = [
        (k, v)
        for k, v in resources_of_type(s.ingestion_json, "AWS::Lambda::Function").items()
        if v["Properties"]["Handler"] == handler
    ]
    return match


def _queue_name_dimension(alarm: dict[str, Any]) -> Any:
    (dimension,) = alarm["Dimensions"]
    assert dimension["Name"] == "QueueName"
    return dimension["Value"]


# --- alarms ---------------------------------------------------------------------------


def test_exactly_three_alarms(synthesized: Synthesized) -> None:
    assert len(resources_of_type(synthesized.ingestion_json, "AWS::CloudWatch::Alarm")) == 3


def test_dlq_alarm_fires_on_first_message(synthesized: Synthesized) -> None:
    alarm = _alarm(synthesized, "ingestion-dlq-not-empty")
    assert alarm["Namespace"] == "AWS/SQS"
    assert alarm["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert _queue_name_dimension(alarm) == {
        "Fn::GetAtt": [_queue_id(synthesized, "file-processing-dlq"), "QueueName"]
    }
    assert alarm["Period"] == 60
    assert alarm["EvaluationPeriods"] == 1
    assert alarm["DatapointsToAlarm"] == 1
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"


def test_backlog_alarm_on_message_age(synthesized: Synthesized) -> None:
    alarm = _alarm(synthesized, "ingestion-queue-oldest-message-age")
    assert alarm["MetricName"] == "ApproximateAgeOfOldestMessage"
    assert _queue_name_dimension(alarm) == {
        "Fn::GetAtt": [_queue_id(synthesized, "file-processing-queue"), "QueueName"]
    }
    assert alarm["Threshold"] == 900
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"


def test_worker_errors_alarm_over_five_minutes(synthesized: Synthesized) -> None:
    alarm = _alarm(synthesized, "ingestion-worker-errors")
    worker_id, _ = _function(synthesized, "ingestion.handlers.etl_worker.handler")
    assert alarm["Namespace"] == "AWS/Lambda"
    assert alarm["MetricName"] == "Errors"
    assert alarm["Dimensions"] == [{"Name": "FunctionName", "Value": {"Ref": worker_id}}]
    assert alarm["Period"] == 300
    assert alarm["Statistic"] == "Sum"
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"


def test_every_alarm_notifies_the_ops_alerts_topic(synthesized: Synthesized) -> None:
    for alarm in resources_of_type(synthesized.ingestion_json, "AWS::CloudWatch::Alarm").values():
        props = alarm["Properties"]
        for key in ("AlarmActions", "OKActions"):
            (action,) = props[key]
            # The topic lives in SchoolSharedStack, so it arrives as a cross-stack import.
            assert "Fn::ImportValue" in action
            assert "OpsAlerts" in str(action["Fn::ImportValue"])


# --- replay function --------------------------------------------------------------------


def _replay_statements(s: Synthesized) -> list[dict[str, Any]]:
    _, function = _function(s, "ingestion.handlers.dlq_replay.handler")
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    statements: list[dict[str, Any]] = []
    for policy in resources_of_type(s.ingestion_json, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in policy["Properties"]["Roles"]:
            statements += policy["Properties"]["PolicyDocument"]["Statement"]
    return statements


def test_replay_function_settings(synthesized: Synthesized) -> None:
    _, function = _function(synthesized, "ingestion.handlers.dlq_replay.handler")
    props = function["Properties"]
    assert props["Architectures"] == ["arm64"]
    assert set(props["Environment"]["Variables"]) >= {"DLQ_URL", "DLQ_ARN", "MAIN_QUEUE_ARN"}
    assert "TracingConfig" not in props  # no X-Ray: keeps the role free of Resource '*'


def test_replay_role_permissions(synthesized: Synthesized) -> None:
    dlq = {"Fn::GetAtt": [_queue_id(synthesized, "file-processing-dlq"), "Arn"]}
    main = {"Fn::GetAtt": [_queue_id(synthesized, "file-processing-queue"), "Arn"]}
    by_resource: dict[str, set[str]] = {}
    for statement in _replay_statements(synthesized):
        key = str(statement["Resource"])
        by_resource.setdefault(key, set()).update(as_list(statement["Action"]))

    assert by_resource.pop(str(dlq)) == {
        "sqs:ReceiveMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:StartMessageMoveTask",
        "sqs:ListMessageMoveTasks",
        "sqs:DeleteMessage",
    }
    assert by_resource.pop(str(main)) == {"sqs:SendMessage"}
    ((_, log_actions),) = by_resource.items()  # only its own log group remains
    assert log_actions == {"logs:CreateLogStream", "logs:PutLogEvents"}


def test_replay_role_has_no_wildcard_resources(synthesized: Synthesized) -> None:
    for statement in _replay_statements(synthesized):
        assert "*" not in str(statement["Resource"])


# --- alert delivery ------------------------------------------------------------------


def _shared_template(fake_layer_dir: Path, **context: Any) -> dict[str, Any]:
    mp = pytest.MonkeyPatch()
    mp.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    mp.delenv("CDK_DEFAULT_REGION", raising=False)
    try:
        app = cdk.App(context=context)
        stacks = build_stacks(app, layer_asset_path=fake_layer_dir)
        return dict(app.synth().get_stack_by_name(stacks.shared.stack_name).template)
    finally:
        mp.undo()


def test_no_subscription_by_default(synthesized: Synthesized) -> None:
    assert resources_of_type(synthesized.shared_json, "AWS::SNS::Subscription") == {}


def test_alert_email_context_adds_an_email_subscription(fake_layer_dir: Path) -> None:
    template = _shared_template(fake_layer_dir, alertEmail="ops@example.org")
    (subscription,) = resources_of_type(template, "AWS::SNS::Subscription").values()
    assert subscription["Properties"]["Protocol"] == "email"
    assert subscription["Properties"]["Endpoint"] == "ops@example.org"

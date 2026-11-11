from __future__ import annotations

from aws_cdk.assertions import Match

from tests.infra.conftest import Synthesized, resources_of_type


def test_event_bus_is_named_school_event_bus(synthesized: Synthesized) -> None:
    synthesized.shared.resource_count_is("AWS::Events::EventBus", 1)
    synthesized.shared.has_resource_properties("AWS::Events::EventBus", {"Name": "SchoolEventBus"})


def test_archive_keeps_events_for_30_days(synthesized: Synthesized) -> None:
    synthesized.shared.has_resource_properties(
        "AWS::Events::Archive",
        {"RetentionDays": 30, "SourceArn": Match.any_value()},
    )


def test_ops_alerts_topic_is_encrypted_and_enforces_ssl(synthesized: Synthesized) -> None:
    synthesized.shared.has_resource_properties(
        "AWS::SNS::Topic", {"TopicName": "ops-alerts", "KmsMasterKeyId": Match.any_value()}
    )
    synthesized.shared.has_resource_properties(
        "AWS::SNS::TopicPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )


def test_topic_key_rotates_and_lets_cloudwatch_alarms_publish(synthesized: Synthesized) -> None:
    keys = resources_of_type(synthesized.shared_json, "AWS::KMS::Key")
    assert len(keys) == 1
    (key,) = keys.values()
    assert key["Properties"]["EnableKeyRotation"] is True
    cloudwatch = [
        s
        for s in key["Properties"]["KeyPolicy"]["Statement"]
        if s.get("Principal", {}).get("Service") == "cloudwatch.amazonaws.com"
    ]
    assert len(cloudwatch) == 1
    assert set(cloudwatch[0]["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey*"}
    # A source-account condition silently breaks alarm delivery (seen in a real deploy).
    assert "Condition" not in cloudwatch[0]


def test_cloudwatch_alarms_may_publish_to_the_topic(synthesized: Synthesized) -> None:
    """enforce_ssl's topic policy replaces SNS's default policy, so without an explicit
    allow, alarms fail with "not authorized to perform SNS:Publish" (seen in a real deploy)."""
    (policy,) = resources_of_type(synthesized.shared_json, "AWS::SNS::TopicPolicy").values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    (allow,) = [
        s
        for s in statements
        if s["Effect"] == "Allow"
        and s.get("Principal", {}).get("Service") == "cloudwatch.amazonaws.com"
    ]
    assert allow["Action"] == "sns:Publish"
    assert "aws:SourceAccount" in allow["Condition"]["StringEquals"]
    assert ":alarm:*" in str(allow["Condition"]["ArnLike"]["aws:SourceArn"])
    # The SSL-only deny is still there alongside it.
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )

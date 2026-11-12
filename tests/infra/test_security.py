"""Cross-cutting checks: IAM hygiene and cdk-nag acknowledgements."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.infra.conftest import Synthesized, as_list

# Spec section 4: the only acknowledgements allowed without asking first.
ALLOWED_ACKNOWLEDGEMENTS: dict[str, set[str]] = {
    # rule id -> resource types it may appear on
    "AwsSolutions-S1": {"AWS::S3::Bucket"},
    # Knowledge spec: Cognito findings that conflict with the stated design (MFA optional).
    "AwsSolutions-COG2": {"AWS::Cognito::UserPool"},
    # Owner decision: Essentials plan rather than Plus (cost); see user_directory.py.
    "AwsSolutions-COG8": {"AWS::Cognito::UserPool"},
    "AwsSolutions-IAM5": {"AWS::IAM::Policy"},
    "AwsSolutions-L1": {"AWS::Lambda::Function"},
}

POLICY_DOCUMENT_KEYS = ("PolicyDocument", "KeyPolicy", "AssumeRolePolicyDocument")


def _templates(s: Synthesized) -> list[dict[str, Any]]:
    return [s.shared_json, s.ingestion_json, s.knowledge_json]


def _statements(template: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for resource in template["Resources"].values():
        props = resource.get("Properties", {})
        docs = [props[k] for k in POLICY_DOCUMENT_KEYS if k in props]
        if resource["Type"] == "AWS::IAM::Role":  # inline role policies
            docs += [p["PolicyDocument"] for p in props.get("Policies", [])]
        for doc in docs:
            yield from doc["Statement"]


def test_no_statement_allows_every_action(synthesized: Synthesized) -> None:
    for template in _templates(synthesized):
        for statement in _statements(template):
            assert "*" not in as_list(statement.get("Action", [])), statement


def test_no_aws_managed_policies_are_attached(synthesized: Synthesized) -> None:
    for template in _templates(synthesized):
        for resource in template["Resources"].values():
            assert "ManagedPolicyArns" not in resource.get("Properties", {}), resource


@pytest.mark.parametrize("which", ["shared_json", "ingestion_json", "knowledge_json"])
def test_every_nag_acknowledgement_is_allowed_and_explained(
    synthesized: Synthesized, which: str
) -> None:
    template: dict[str, Any] = getattr(synthesized, which)
    for logical_id, resource in template["Resources"].items():
        rules = resource.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        for rule in rules:
            rule_id = rule["id"].split("[", 1)[0]
            assert rule_id in ALLOWED_ACKNOWLEDGEMENTS, f"{logical_id}: {rule_id} not approved"
            assert resource["Type"] in ALLOWED_ACKNOWLEDGEMENTS[rule_id], (
                f"{rule_id} acknowledged on {resource['Type']} ({logical_id}): too broad"
            )
            assert len(rule["reason"].strip()) >= 20, f"{logical_id}: {rule_id} needs a reason"


def test_no_stack_level_acknowledgements(synthesized: Synthesized) -> None:
    """Acknowledgements must sit on resources, not on whole stacks."""
    for template in _templates(synthesized):
        assert "cdk_nag" not in template.get("Metadata", {})


def test_s1_is_acknowledged_only_on_the_two_service_buckets(synthesized: Synthesized) -> None:
    holders = [
        (logical_id, r["Type"])
        for template in _templates(synthesized)
        for logical_id, r in template["Resources"].items()
        if any(
            rule["id"] == "AwsSolutions-S1"
            for rule in r.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        )
    ]
    assert len(holders) == 2
    assert {t for _, t in holders} == {"AWS::S3::Bucket"}

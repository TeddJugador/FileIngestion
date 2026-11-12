"""SharedStack: the system's Cognito user pool."""

from __future__ import annotations

from typing import Any

from tests.infra.conftest import Synthesized, SynthFn, resources_of_type


def _pool(template: dict[str, Any]) -> dict[str, Any]:
    (pool,) = resources_of_type(template, "AWS::Cognito::UserPool").values()
    return dict(pool["Properties"])


def _clients(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        c["Properties"]["ClientName"]: c["Properties"]
        for c in resources_of_type(template, "AWS::Cognito::UserPoolClient").values()
    }


def test_admins_create_users_and_users_sign_in_with_email(synthesized: Synthesized) -> None:
    pool = _pool(synthesized.shared_json)
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["UsernameAttributes"] == ["email"]
    assert pool["AutoVerifiedAttributes"] == ["email"]


def test_strong_password_policy(synthesized: Synthesized) -> None:
    policy = _pool(synthesized.shared_json)["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] >= 12
    for requirement in ("RequireLowercase", "RequireUppercase", "RequireNumbers", "RequireSymbols"):
        assert policy[requirement] is True


def test_mfa_optional_with_totp_only(synthesized: Synthesized) -> None:
    pool = _pool(synthesized.shared_json)
    assert pool["MfaConfiguration"] == "OPTIONAL"
    assert pool["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]


def test_school_id_attribute_is_immutable(synthesized: Synthesized) -> None:
    (attribute,) = [a for a in _pool(synthesized.shared_json)["Schema"] if a["Name"] == "school_id"]
    assert attribute["Mutable"] is False
    assert attribute["AttributeDataType"] == "String"


def test_three_groups(synthesized: Synthesized) -> None:
    groups = resources_of_type(synthesized.shared_json, "AWS::Cognito::UserPoolGroup")
    assert sorted(g["Properties"]["GroupName"] for g in groups.values()) == [
        "admin",
        "parent",
        "staff",
    ]


def test_web_client_uses_code_flow_without_a_secret(synthesized: Synthesized) -> None:
    web = _clients(synthesized.shared_json)["school-web"]
    assert web.get("GenerateSecret", False) is False
    assert web["AllowedOAuthFlows"] == ["code"]
    assert web["AllowedOAuthFlowsUserPoolClient"] is True
    assert "ALLOW_USER_PASSWORD_AUTH" not in web["ExplicitAuthFlows"]
    assert web["PreventUserExistenceErrors"] == "ENABLED"


def test_password_test_client_exists_in_dev(synthesized: Synthesized) -> None:
    test = _clients(synthesized.shared_json)["school-test-dev-only"]
    assert "ALLOW_USER_PASSWORD_AUTH" in test["ExplicitAuthFlows"]
    assert test.get("GenerateSecret", False) is False


def test_no_password_test_client_outside_dev(synth: SynthFn) -> None:
    clients = _clients(synth(stage="prod")["shared"])
    assert "school-test-dev-only" not in clients
    assert all("ALLOW_USER_PASSWORD_AUTH" not in c["ExplicitAuthFlows"] for c in clients.values())

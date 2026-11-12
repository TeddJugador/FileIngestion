"""Phase (e) infrastructure: the HTTP API, its authorizer, and the query Lambda."""

from __future__ import annotations

import json
from typing import Any

from tests.infra.conftest import Synthesized, SynthFn, as_list, resources_of_type

ROUTES = {
    "POST /ask",
    "POST /feedback",
    "GET /documents",
    "POST /documents/{documentId}/retire",
}


def _query_function(template: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (match,) = [
        (k, v)
        for k, v in resources_of_type(template, "AWS::Lambda::Function").items()
        if v["Properties"]["Handler"] == "knowledge.handlers.api.handler"
    ]
    return match


def _statements(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _, function = _query_function(template)
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return {
        s["Sid"]: s
        for p in resources_of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in p["Properties"]["Roles"]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "Sid" in s
    }


def test_exactly_the_four_routes(synthesized: Synthesized) -> None:
    routes = resources_of_type(synthesized.knowledge_json, "AWS::ApiGatewayV2::Route").values()
    assert {r["Properties"]["RouteKey"] for r in routes} == ROUTES


def test_every_route_requires_a_cognito_jwt(synthesized: Synthesized) -> None:
    (authorizer_id, authorizer) = next(
        iter(resources_of_type(synthesized.knowledge_json, "AWS::ApiGatewayV2::Authorizer").items())
    )
    props = authorizer["Properties"]
    assert props["AuthorizerType"] == "JWT"
    assert props["IdentitySource"] == ["$request.header.Authorization"]
    assert "cognito-idp" in json.dumps(props["JwtConfiguration"]["Issuer"])
    assert len(props["JwtConfiguration"]["Audience"]) == 2  # web client + dev test client
    for route in resources_of_type(synthesized.knowledge_json, "AWS::ApiGatewayV2::Route").values():
        assert route["Properties"]["AuthorizationType"] == "JWT"
        assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer_id}


def test_only_the_web_client_is_trusted_outside_dev(synth: SynthFn) -> None:
    template = synth(stage="prod")["knowledge"]
    (authorizer,) = resources_of_type(template, "AWS::ApiGatewayV2::Authorizer").values()
    assert len(authorizer["Properties"]["JwtConfiguration"]["Audience"]) == 1


def test_throttling_cors_and_access_logs(synthesized: Synthesized) -> None:
    (stage,) = resources_of_type(synthesized.knowledge_json, "AWS::ApiGatewayV2::Stage").values()
    props = stage["Properties"]
    assert props["StageName"] == "$default"
    assert props["DefaultRouteSettings"]["ThrottlingBurstLimit"] == 20
    assert props["DefaultRouteSettings"]["ThrottlingRateLimit"] == 10
    log_format = props["AccessLogSettings"]["Format"]
    assert "requestId" in log_format
    for private in ("sourceIp", "identity", "requestBody", "queryString"):
        assert private not in log_format

    (api,) = resources_of_type(synthesized.knowledge_json, "AWS::ApiGatewayV2::Api").values()
    cors = api["Properties"]["CorsConfiguration"]
    assert cors["AllowOrigins"] == ["http://localhost:3000"]  # exact origins, never "*"
    assert set(cors["AllowMethods"]) == {"GET", "POST", "OPTIONS"}


def test_cors_origins_come_from_config(synth: SynthFn) -> None:
    template = synth(corsAllowedOrigins=["https://app.riverside.example"])["knowledge"]
    (api,) = resources_of_type(template, "AWS::ApiGatewayV2::Api").values()
    assert api["Properties"]["CorsConfiguration"]["AllowOrigins"] == [
        "https://app.riverside.example"
    ]


def test_query_function_settings(synthesized: Synthesized) -> None:
    _, function = _query_function(synthesized.knowledge_json)
    props = function["Properties"]
    assert props["MemorySize"] == 1024
    assert props["Timeout"] == 25  # under API Gateway's 30 s limit
    assert props["Architectures"] == ["arm64"]
    assert "ProvisionedConcurrencyConfig" not in json.dumps(synthesized.knowledge_json)
    env = props["Environment"]["Variables"]
    assert env["GENERATION_PROVIDER"] == "CLAUDE"
    assert env["GENERATION_MODEL"] == "claude-haiku-5-5"
    assert env["SIMILARITY_THRESHOLD"] == "0.35"
    assert not any("secret" in json.dumps(v).lower() for v in env.values())


def test_query_permissions(synthesized: Synthesized) -> None:
    sids = _statements(synthesized.knowledge_json)
    assert set(as_list(sids["QueryVectors"]["Action"])) == {
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
    }
    assert as_list(sids["RetireDeletesVectors"]["Action"]) == ["s3vectors:DeleteVectors"]
    assert set(as_list(sids["RetireDeletesFiles"]["Action"])) == {"s3:GetObject", "s3:DeleteObject"}
    assert set(as_list(sids["DocumentRegistry"]["Action"])) == {
        "dynamodb:BatchGetItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem",
    }
    registry_resources = json.dumps(sids["DocumentRegistry"]["Resource"])
    assert "/index/BySchoolStatus" in registry_resources  # the one index, not index/*
    assert "index/*" not in registry_resources
    assert set(as_list(sids["QueryLog"]["Action"])) == {"dynamodb:GetItem", "dynamodb:PutItem"}
    secrets = json.dumps(sids["ProviderKeys"]["Resource"])
    assert ":secret:knowledge/gemini-??????" in secrets
    assert ":secret:knowledge/anthropic-??????" in secrets


def test_api_cannot_read_the_ingestion_bucket(synthesized: Synthesized) -> None:
    assert "school-ingestion-" not in json.dumps(_statements(synthesized.knowledge_json))


def test_gemini_for_both_needs_only_one_secret(synth: SynthFn) -> None:
    template = synth(generationProvider="GEMINI", generationModel="gemini-3.5-flash-lite")[
        "knowledge"
    ]
    resources = _statements(template)["ProviderKeys"]["Resource"]
    assert len(as_list(resources)) == 1


def test_outputs_for_clients_and_scripts(synthesized: Synthesized) -> None:
    assert "ApiUrl" in synthesized.knowledge_json["Outputs"]
    assert {"UserPoolId", "WebClientId", "TestClientId"} <= set(synthesized.shared_json["Outputs"])

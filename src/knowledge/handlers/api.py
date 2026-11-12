"""Query API (HTTP API + Cognito JWT authorizer). Routing, validation and identity only.

Identity comes from the verified token's claims, never from the body. API Gateway
checks the token; group-level permissions (admin routes) are checked here, because an
HTTP API JWT authorizer can only check scopes, not Cognito groups.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import Annotated, Any, Literal

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from knowledge.events import EventPublisher
from knowledge.handlers.indexer import require_s3vectors
from knowledge.identity import Caller, NotAuthorizedError, caller_from_claims
from knowledge.metrics import PowertoolsMetricsSink
from knowledge.providers.factory import ProviderSettings, build_embedder, build_generator
from knowledge.providers.http import QUERY_RETRIES
from knowledge.query import (
    DEFAULT_TOP_K,
    DocumentNotFoundError,
    FeedbackNotAllowedError,
    ModelMismatchError,
    Outcome,
    QueryService,
    QuerySettings,
)
from knowledge.query_log import QueryLog
from knowledge.registry import Registry
from knowledge.sources import SourceStore
from knowledge.vector_store import S3VectorsStore
from knowledge.versions import VersionCleaner

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
AWS_RETRIES = Config(retries={"mode": "standard", "max_attempts": 3})


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: Annotated[str, Field(min_length=3, max_length=1000)]
    topK: Annotated[StrictInt, Field(ge=1, le=10)] = DEFAULT_TOP_K


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queryId: Annotated[str, Field(pattern=r"^qry-[0-9A-Z]{26}$")]
    helpful: StrictBool
    reasonCode: Literal["WRONG", "INCOMPLETE", "NOT_RELEVANT", "OUTDATED"] | None = None


@cache
def service() -> QueryService:
    env = os.environ
    session = boto3.session.Session()
    require_s3vectors(session)
    metrics = PowertoolsMetricsSink(env["POWERTOOLS_METRICS_NAMESPACE"])
    providers = ProviderSettings.from_env(env)
    registry = Registry(session.client("dynamodb", config=AWS_RETRIES), env["REGISTRY_TABLE_NAME"])
    store = S3VectorsStore(
        session.client("s3vectors", config=AWS_RETRIES),
        bucket_name=env["VECTOR_BUCKET_NAME"],
        index_name=env["VECTOR_INDEX_NAME"],
    )
    sources = SourceStore(
        session.client("s3", config=AWS_RETRIES),
        knowledge_bucket=env["KNOWLEDGE_BUCKET_NAME"],
        ingestion_bucket="",  # the API never reads ingestion's staging area
        max_bytes=0,
    )
    return QueryService(
        settings=QuerySettings(
            index_name=env["VECTOR_INDEX_NAME"],
            embedding_model_id=providers.embedding_model_id,
            generation_provider=providers.generation_provider,
            generation_model=providers.generation_model,
            similarity_threshold=float(env.get("SIMILARITY_THRESHOLD", "0.35")),
        ),
        registry=registry,
        store=store,
        embedder=build_embedder(providers, policy=QUERY_RETRIES, observer=metrics),
        generator=build_generator(providers, policy=QUERY_RETRIES, observer=metrics),
        publisher=EventPublisher(session.client("events"), env["EVENT_BUS_NAME"]),
        query_log=QueryLog(
            session.client("dynamodb", config=AWS_RETRIES), env["QUERY_LOG_TABLE_NAME"]
        ),
        cleaner=VersionCleaner(registry, sources, store, env["VECTOR_INDEX_NAME"]),
        metrics=metrics,
        logger=logger,
    )


def _caller() -> Caller:
    claims = app.current_event.request_context.authorizer.jwt_claim or {}
    try:
        return caller_from_claims(claims)
    except NotAuthorizedError:
        raise ForbiddenError("Your account isn't set up for this service yet.") from None


def _body(model: type[BaseModel]) -> Any:  # noqa: ANN401
    try:
        raw = app.current_event.json_body
    except (json.JSONDecodeError, TypeError):
        raise BadRequestError("Request body must be JSON.") from None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # Field names and rules only: never echo the (possibly personal) input back.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors()
        )
        raise BadRequestError(problems) from None


@app.post("/ask")
@tracer.capture_method
def ask() -> Response[str]:
    caller = _caller()
    body: AskBody = _body(AskBody)
    try:
        result = service().ask(caller, body.question, body.topK)
    except ModelMismatchError:
        raise ServiceUnavailableError(
            "The document index is being rebuilt. Please try again later."
        ) from None
    status = 503 if result.outcome is Outcome.PROVIDER_ERROR else 200
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(result.to_dict()),
    )


@app.post("/feedback")
@tracer.capture_method
def feedback() -> dict[str, Any]:
    caller = _caller()
    body: FeedbackBody = _body(FeedbackBody)
    try:
        service().feedback(caller, body.queryId, body.helpful, body.reasonCode)
    except FeedbackNotAllowedError:
        raise NotFoundError("Query not found.") from None
    return {"queryId": body.queryId, "recorded": True}


def _admin() -> Caller:
    caller = _caller()
    if not caller.is_admin:
        raise ForbiddenError("Only school admins can manage documents.")
    return caller


@app.get("/documents")
@tracer.capture_method
def list_documents() -> dict[str, Any]:
    return {"documents": service().list_documents(_admin())}


@app.post("/documents/<document_id>/retire")
@tracer.capture_method
def retire(document_id: str) -> dict[str, Any]:
    caller = _admin()
    try:
        return service().retire(caller, document_id)
    except DocumentNotFoundError:
        raise NotFoundError("No active document with that ID.") from None


@logger.inject_lambda_context(clear_state=True, correlation_id_path="requestContext.requestId")
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)

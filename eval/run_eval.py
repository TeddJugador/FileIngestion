"""Evaluate the deployed dev stage against the fictional corpus and questions.

    python eval/run_eval.py --label "Gemini embeddings + Gemini 3.5 Flash-Lite"
    python eval/run_eval.py --label "..." --skip-upload --update-readme
    python eval/run_eval.py --cleanup-only        # retire the evaluation documents

Uploads the corpus through the ingestion bucket (school SCH-900), waits until every
document is active in the configured index, creates temporary parent and staff users,
then for each question:
  - measures retrieval with QueryService.retrieve (the code path /ask uses), and
  - asks the real API over HTTP as that role,
and prices each answer from its QuestionAnswered event. Writes eval/results/<slug>.json
and .md. Uses your AWS credentials and the provider keys in Secrets Manager.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "eval")]

import boto3  # noqa: E402
import yaml  # noqa: E402
from aws_lambda_powertools import Logger  # noqa: E402
from corpus_tools import Manifest, load_manifest  # noqa: E402
from metrics import QuestionResult, Retrieved, summarise  # noqa: E402

from knowledge.identity import Caller, Role  # noqa: E402
from knowledge.providers.factory import ProviderSettings, build_embedder  # noqa: E402
from knowledge.providers.fakes import FakeGenerator  # noqa: E402
from knowledge.providers.http import INDEXING_RETRIES  # noqa: E402
from knowledge.query import QueryService, QuerySettings  # noqa: E402
from knowledge.registry import Registry  # noqa: E402
from knowledge.tokens import estimate_tokens  # noqa: E402
from knowledge.vector_store import S3VectorsStore  # noqa: E402
from tests.integration.capture import EventCapture, capture_events  # noqa: E402

EVAL_DIR = ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
INDEX_TIMEOUT_SECONDS = 600
EVENTS_TIMEOUT_SECONDS = 120


# --- deployed stack --------------------------------------------------------------------


def outputs(stack: str) -> dict[str, str]:
    description = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


def provider_settings() -> ProviderSettings:
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    return ProviderSettings.from_env(
        {
            "EMBEDDING_PROVIDER": context["embeddingProvider"],
            "EMBEDDING_MODEL": context["embeddingModel"],
            "EMBEDDING_DIMENSION": str(context["embeddingDimension"]),
            "GENERATION_PROVIDER": context["generationProvider"],
            "GENERATION_MODEL": context["generationModel"],
            "GENERATION_THINKING": context.get("generationThinking", ""),
            "GENERATION_EFFORT": context.get("generationEffort", ""),
        }
    )


# --- corpus ------------------------------------------------------------------------------


def upload_corpus(manifest: Manifest, bucket: str) -> None:
    s3 = boto3.client("s3")
    for document in manifest.documents:
        s3.put_object(
            Bucket=bucket,
            Key=f"raw/documents/{manifest.school_id}/{manifest.relative_path(document)}",
            Body=document.path.read_bytes(),
            Metadata={
                "uploaded-by": "USR-eval-harness",
                "audience": ", ".join(document.audience),
                "title": document.title,
            },
        )
        print(f"  uploaded {document.file}")


def wait_until_active(manifest: Manifest, registry: Registry, index_name: str) -> None:
    pending = {
        manifest.document_id(d): f"v-{hashlib.sha256(d.path.read_bytes()).hexdigest()[:12]}"
        for d in manifest.documents
    }
    deadline = time.monotonic() + INDEX_TIMEOUT_SECONDS
    while pending and time.monotonic() < deadline:
        for document_id, version in list(pending.items()):
            record = registry.get_document(manifest.school_id, document_id)
            if record and record.active_version == version and record.index_name == index_name:
                del pending[document_id]
            elif record and record.status == "FAILED" and not record.active_version:
                sys.exit(f"{document_id} failed indexing: check the indexer logs")
        if pending:
            time.sleep(5)
    if pending:
        sys.exit(f"not indexed after {INDEX_TIMEOUT_SECONDS}s: {sorted(pending)}")
    print(f"  all {len(manifest.documents)} documents active in {index_name}")


# --- users and HTTP ----------------------------------------------------------------------


def create_user(
    cognito: Any,  # noqa: ANN401 - boto3 client
    pool_id: str,
    client_id: str,
    group: str,
    school_id: str,
) -> tuple[str, str]:
    email = f"eval-{group}-{uuid.uuid4().hex[:8]}@example.com"
    password = f"Ev{secrets.token_urlsafe(12)}!7a"
    cognito.admin_create_user(
        UserPoolId=pool_id,
        Username=email,
        MessageAction="SUPPRESS",
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:school_id", "Value": school_id},
        ],
    )
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=email, Password=password, Permanent=True
    )
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    token = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )["AuthenticationResult"]["IdToken"]
    return email, token


def call(api_url: str, method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:  # noqa: ANN401
    request = urllib.request.Request(  # noqa: S310 - our own API's https URL
        api_url.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def collect_events(capture: EventCapture, query_ids: set[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + EVENTS_TIMEOUT_SECONDS
    while query_ids - set(found) and time.monotonic() < deadline:
        response = capture.sqs.receive_message(
            QueueUrl=capture.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10
        )
        for message in response.get("Messages", []):
            event = json.loads(message["Body"])
            if event.get("detail-type") == "QuestionAnswered":
                data = event["detail"]["data"]
                found[data["queryId"]] = data
            capture.sqs.delete_message(
                QueueUrl=capture.queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
    return found


# --- the run -----------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest()
    questions = yaml.safe_load((EVAL_DIR / "questions.yaml").read_text())
    pricing = yaml.safe_load((EVAL_DIR / "pricing.yaml").read_text())
    shared, ingestion, knowledge = (
        outputs("SchoolSharedStack"),
        outputs("SchoolIngestionStack"),
        outputs("SchoolKnowledgeStack"),
    )
    settings = provider_settings()
    index_name = knowledge["VectorIndexName"]
    registry = Registry(boto3.client("dynamodb"), knowledge["RegistryTableName"])
    key_of = {manifest.document_id(d): d.key for d in manifest.documents}
    staff_only = {d.key for d in manifest.documents if "PARENT" not in d.audience}

    if not args.skip_upload:
        print("Uploading the corpus through ingestion")
        upload_corpus(manifest, ingestion["BucketName"])
    print("Waiting for indexing")
    wait_until_active(manifest, registry, index_name)

    store = S3VectorsStore(
        boto3.client("s3vectors"), bucket_name=knowledge["VectorBucketName"], index_name=index_name
    )
    service = QueryService(
        settings=QuerySettings(
            index_name=index_name,
            embedding_model_id=settings.embedding_model_id,
            generation_provider=settings.generation_provider,
            generation_model=settings.generation_model,
        ),
        registry=registry,
        store=store,
        embedder=build_embedder(settings, policy=INDEXING_RETRIES),
        generator=FakeGenerator(),  # retrieve() never generates
        publisher=None,  # type: ignore[arg-type]
        query_log=None,  # type: ignore[arg-type]
        cleaner=None,  # type: ignore[arg-type]
        metrics=_NoMetrics(),
        logger=Logger(service="knowledge-eval", level="WARNING"),
    )

    cognito = boto3.client("cognito-idp")
    pool_id, client_id = shared["UserPoolId"], shared["TestClientId"]
    users: list[str] = []
    try:
        tokens: dict[str, str] = {}
        for role in ("parent", "staff"):
            email, tokens[role] = create_user(cognito, pool_id, client_id, role, manifest.school_id)
            users.append(email)
        print("Waiting for the event capture rule to go live")
        with capture_events(source="school.knowledge", school_id=manifest.school_id) as capture:
            results = ask_all(
                args, questions, manifest, service, tokens, knowledge["ApiUrl"], key_of
            )
            print("Collecting token counts from QuestionAnswered events")
            events = collect_events(capture, {r.query_id for r in results if r.query_id})
    finally:
        for user in users:
            cognito.admin_delete_user(UserPoolId=pool_id, Username=user)

    price(results, events, settings, pricing, questions)
    staff_canaries = list(questions.get("staff_only_canaries", []))
    for result in results:
        if result.role == "parent":
            result.staff_canaries = staff_canaries
    summary = summarise(results, staff_only_keys=staff_only, top_k=args.top_k)
    return {
        "label": args.label,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {
            "embedding": settings.embedding_model_id,
            "embedding_dimension": settings.embedding_dimension,
            "generation": settings.generation_model,
            "generation_provider": settings.generation_provider,
            "thinking": settings.generation_thinking,
            "index": index_name,
            "top_k": args.top_k,
        },
        "pricing_checked": str(pricing.get("checked")),
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }


class _NoMetrics:
    def record(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        pass


def ask_all(
    args: argparse.Namespace,
    questions: dict[str, Any],
    manifest: Manifest,
    service: QueryService,
    tokens: dict[str, str],
    api_url: str,
    key_of: dict[str, str],
) -> list[QuestionResult]:
    injection = list(questions.get("injection_canaries", []))
    results = []
    for number, q in enumerate(questions["questions"], start=1):
        result = QuestionResult(
            id=q["id"],
            role=q["role"],
            category=q["category"],
            expected_documents=list(q.get("expected_documents", [])),
            expected_outcome=q.get("expected_outcome"),
            key_facts=list(q.get("key_facts", [])),
            must_not_contain=list(dict.fromkeys([*q.get("must_not_contain", []), *injection])),
        )
        caller = Caller(
            user_id=f"eval-{q['role']}", school_id=manifest.school_id, role=Role(q["role"].upper())
        )
        for hit in service.retrieve(caller, q["question"], args.top_k):
            result.retrieved.append(
                Retrieved(
                    key=key_of.get(str(hit.metadata.get("document_id")), "?"),
                    similarity=round(hit.similarity, 4),
                    audience=list(hit.metadata.get("audience", [])),
                )
            )
        started = time.monotonic()
        status, body = call(
            api_url,
            "POST",
            "/ask",
            tokens[q["role"]],
            {"question": q["question"], "topK": args.top_k},
        )
        result.latency_ms = round((time.monotonic() - started) * 1000)
        result.status = status
        if isinstance(body, dict):
            result.outcome = str(body.get("outcome", ""))
            result.answer = str(body.get("answer", ""))
            result.grounded = bool(body.get("grounded"))
            result.cited = [key_of.get(c["documentId"], "?") for c in body.get("citations", [])]
            result.query_id = str(body.get("queryId", ""))
        print(
            f"  [{number:2}/{len(questions['questions'])}] {q['id']} "
            f"{result.outcome or status} {result.latency_ms} ms"
        )
        results.append(result)
        time.sleep(args.delay)  # stay inside free-tier rate limits
    return results


def price(
    results: list[Any],
    events: dict[str, dict[str, Any]],
    settings: ProviderSettings,
    pricing: dict[str, Any],
    questions: dict[str, Any],
) -> None:
    embed_price = pricing["embedding"].get(settings.embedding_model)
    generation_price = pricing["generation"].get(settings.generation_model)
    text_of = {q["id"]: q["question"] for q in questions["questions"]}
    for result in results:
        event = events.get(result.query_id)
        if event is None or embed_price is None or generation_price is None:
            continue
        result.input_tokens = int(event["inputTokens"])
        result.output_tokens = int(event["outputTokens"])
        embedding_tokens = estimate_tokens(
            f"task: question answering | query: {text_of[result.id]}"
        )
        result.cost_usd = (
            embedding_tokens * embed_price
            + result.input_tokens * generation_price[0]
            + result.output_tokens * generation_price[1]
        ) / 1_000_000


# --- reports -----------------------------------------------------------------------------

METRIC_LABELS = {
    "hit_rate_at_1": "Retrieval hit rate @ 1",
    "hit_rate_at_3": "Retrieval hit rate @ 3",
    "hit_rate_at_6": "Retrieval hit rate @ 6",
    "mrr": "Mean reciprocal rank",
    "multi_document_coverage": "Two-document questions: both retrieved",
    "key_fact_accuracy": "Answers containing the key facts",
    "grounded_rate": "Answers with citations",
    "correct_refusal_rate": "Correct refusals (no sources, no model call)",
    "refusal_rate_including_model": "Refusals including the model declining",
    "audience_leaks": "Audience leaks (must be 0)",
    "injection_resistance": "Injection resistance",
    "provider_errors": "Provider errors",
    "latency_ms_mean": "Latency, mean (ms)",
    "latency_ms_p95": "Latency, p95 (ms)",
    "cost_usd_per_question": "Cost per question (USD, list price)",
}


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def markdown_report(report: dict[str, Any]) -> str:
    config, summary = report["config"], report["summary"]
    lines = [
        f"# Evaluation: {report['label']}",
        "",
        f"Run {report['ran_at']} against `{config['index']}`: embeddings `{config['embedding']}` "
        f"({config['embedding_dimension']} dimensions), generation `{config['generation']}` "
        f"(thinking: {config['thinking'] or 'default'}), top-k {config['top_k']}. "
        f"Prices checked {report['pricing_checked']}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        *(
            f"| {METRIC_LABELS[k]} | {summary[k] if summary[k] is not None else 'n/a'} |"
            for k in METRIC_LABELS
            if k in summary
        ),
        "",
        "## Per question",
        "",
        "| ID | Category | Role | Outcome | Rank | Key facts | Forbidden text | Latency (ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['id']} | {r['category']} | {r['role']} | {r['outcome'] or r['status']} | "
            f"{r['rank'] or '-'} | {'yes' if r['facts_found'] else 'no'} | "
            f"{', '.join(r['forbidden_found']) or '-'} | {r['latency_ms']} |"
        )
    failures = [
        r
        for r in report["results"]
        if r["forbidden_found"]
        or (r["expected_outcome"] == "ANSWERED" and not r["facts_found"])
        or (
            r["expected_outcome"] == "NO_RELEVANT_SOURCES" and r["outcome"] != "NO_RELEVANT_SOURCES"
        )
    ]
    if failures:
        lines += [
            "",
            "## Answers to review",
            "",
            "Every answer that missed a check, in full (the corpus is fictional).",
            "",
        ]
        for r in failures:
            lines += [f"**{r['id']}** ({r['category']}, {r['outcome']}): {r['answer']}", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", help="name for this configuration, e.g. 'Gemini + Gemini'")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between questions")
    parser.add_argument("--skip-upload", action="store_true", help="corpus already uploaded")
    parser.add_argument("--update-readme", action="store_true", help="refresh the README table")
    parser.add_argument("--cleanup-only", action="store_true", help="retire the eval documents")
    args = parser.parse_args()

    if args.cleanup_only:
        cleanup()
        return
    if not args.label:
        parser.error("--label is required")
    report = run(args)
    RESULTS_DIR.mkdir(exist_ok=True)
    base = RESULTS_DIR / slug(args.label)
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    base.with_suffix(".md").write_text(markdown_report(report))
    print(f"\nWrote {base.with_suffix('.md').relative_to(ROOT)}")
    for key, label in METRIC_LABELS.items():
        if key in report["summary"]:
            print(f"  {label}: {report['summary'][key]}")
    if args.update_readme:
        from summarize import update_readme

        update_readme()
        print("README evaluation table updated")


def cleanup() -> None:
    manifest = load_manifest()
    shared, knowledge = outputs("SchoolSharedStack"), outputs("SchoolKnowledgeStack")
    cognito = boto3.client("cognito-idp")
    email, token = create_user(
        cognito, shared["UserPoolId"], shared["TestClientId"], "admin", manifest.school_id
    )
    try:
        for document in manifest.documents:
            status, _ = call(
                knowledge["ApiUrl"],
                "POST",
                f"/documents/{manifest.document_id(document)}/retire",
                token,
            )
            print(f"  {document.file}: {'retired' if status == 200 else f'not active ({status})'}")
    finally:
        cognito.admin_delete_user(UserPoolId=shared["UserPoolId"], Username=email)


if __name__ == "__main__":
    main()

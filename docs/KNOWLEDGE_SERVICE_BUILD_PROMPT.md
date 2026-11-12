# Build Prompt: Knowledge Service (Retrieval-Augmented Generation)

> **How to use this file:** give it to a coding agent (e.g. Claude Code) as the task specification, together with `EVENT_CATALOGUE.md` and `INGESTION_BUILD_PROMPT.md` (for shared conventions). Section 11 tells the agent how to work.

## 1. Context

You are building the **knowledge service** for a multi-tenant school management system on AWS. Services communicate only through the shared EventBridge bus `SchoolEventBus`, as defined in `EVENT_CATALOGUE.md`.

The service lets staff and parents ask natural-language questions about a school's documents (handbooks, policies, fee rules, curriculum guides) and get answers grounded in those documents, with citations to the document and page.

It has two halves:

- **Indexing (asynchronous):** consumes `DocumentAccepted` from the ingestion service, extracts text, chunks it, embeds the chunks, and stores the vectors.
- **Querying (synchronous):** an authenticated HTTP API that embeds a question, retrieves the most relevant chunks the caller is allowed to see, and asks a language model to answer from them.

This replicates what a managed service like Amazon Bedrock Knowledge Bases does, built from parts, with the embedding and generation providers swappable through configuration.

### Data boundary (non-negotiable)

Only school documents are indexed. **Student records, marks, fee balances and any other personal data are never embedded or sent to a model provider.** Questions may contain personal details typed by users, so question and answer text is never logged, never stored, and never placed in events.

The ingestion bucket's other `processed/` prefixes hold claim-check files with learners' personal data (rosters, marks). This service reads **only** `processed/documents/*`, and its IAM policy enforces that.

## 2. Technical stack (fixed)

- **IaC:** AWS CDK v2 in **Python**.
- **Runtime:** the newest Python Lambda runtime that both AWS Lambda and Powertools for AWS Lambda (Python) support at build time (3.13 or later), `arm64`. Use the same runtime as the ingestion service.
- **Libraries:** AWS Lambda Powertools (logging, metrics, tracing, batch processing, idempotency, parameters, event handler), `pydantic` v2, `pypdf` (PDF text), `python-docx` (DOCX text), `httpx` or the providers' official SDKs, `python-ulid` for event IDs, `boto3`.
- **Dependency packaging:** as in the ingestion service, build one arm64 dependencies layer with `pip install --platform manylinux2014_aarch64 --implementation cp --python-version <runtime version> --only-binary=:all: --target <dir>`, containing Powertools, pydantic, pypdf, python-docx, the provider SDKs and python-ulid. Several of these are compiled (`pydantic-core`, and `lxml`, which `python-docx` depends on), so a plain `pip install` on an x86 machine produces a layer that fails to import on arm64. Do not also attach the public Powertools layer. `boto3` comes from the runtime; check that the runtime's `boto3` includes the `s3vectors` client, and bundle a newer `boto3` in the layer if it doesn't.
- **Vector store:** Amazon S3 Vectors (vector bucket + vector index).
- **Document store:** a knowledge-owned S3 bucket holding this service's own copy of each indexed document's source (see section 4). The ingestion service's staged copies expire after 45 days, so they are an input, not storage.
- **Metadata store:** DynamoDB, on-demand.
- **API:** API Gateway HTTP API with a Cognito JWT authorizer.
- **Secrets:** AWS Secrets Manager, read through the Powertools parameters utility with caching.
- **Quality:** `pytest`, `moto` where it supports the service, CDK `assertions`, `cdk-nag`, `ruff`, `mypy --strict` on `src/`.

## 3. Provider abstraction

All model calls go through two interfaces in `src/knowledge/providers/`:

```python
class Embedder(Protocol):
    model_id: str  # e.g. "gemini/<model-name>"
    dimension: int
    max_batch_size: int
    max_input_tokens: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class Generator(Protocol):
    provider: str  # "CLAUDE" | "GEMINI"
    model_id: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...
```

Implementations for v1:

| Interface | Implementations |
|---|---|
| `Embedder` | `GeminiEmbedder`, `VoyageEmbedder` |
| `Generator` | `ClaudeGenerator`, `GeminiGenerator` |
| Both (tests only) | `FakeEmbedder` (deterministic hash-based vectors), `FakeGenerator` (echoes cited chunk IDs) |

Rules:

- **Model names are configuration, never code.** They come from `cdk.json` context (`embeddingProvider`, `embeddingModel`, `embeddingDimension`, `generationProvider`, `generationModel`) and reach Lambdas as environment variables. Before choosing defaults, check each provider's current documentation for model names, output dimensions, batch limits and rate limits; model names change and older ones get deprecated. Record what you chose and the date in the README.
- **Use the providers' retrieval task hints:** Gemini's task type for retrieval documents versus retrieval queries, and Voyage's `input_type` of `document` versus `query`. Embedding documents and queries the same way measurably hurts retrieval.
- **Request the configured output dimension explicitly** where the provider supports reduced dimensions, and assert the returned vector length equals `embeddingDimension`.
- **Retries:** exponential backoff with jitter on HTTP 429 and 5xx, honouring any `Retry-After` header; max 5 attempts for indexing, max 2 for queries (a user is waiting). Non-retryable 4xx raise immediately.
- **Timeouts:** 20 s per embedding call, 20 s per generation call.
- **API keys** live in one Secrets Manager secret per provider (`knowledge/gemini`, `knowledge/voyage`, `knowledge/anthropic`), fetched at cold start and cached for 5 minutes. Never in environment variables, code, logs or Git.

### Embedding model and index are bound together

A vector index only makes sense for the model and dimension that produced its vectors. Therefore:

- The index name encodes them: `docs-<provider>-<dimension>-v<n>`, e.g. `docs-gemini-768-v1`.
- The registry stores `embeddingModel` per indexed version, and the query path refuses to run (and alarms) if the configured embedder differs from the index's model.
- Changing embedding model means creating a new index and re-indexing everything (section 8 `reindex` action), never mixing vectors.

## 4. Infrastructure

### Extend `SharedStack`

Add a Cognito user pool for the whole system:

- Email sign-in, self sign-up **disabled** (admins create users), strong password policy, MFA optional (TOTP).
- Groups: `admin`, `staff`, `parent`.
- Custom attribute `custom:school_id` (immutable).
- An app client for the future web front end (authorization code flow with PKCE, no client secret) and one for testing (`USER_PASSWORD_AUTH`, dev stage only).
- Expose the pool and clients as stack properties.

### `KnowledgeStack`

Receives the bus, ops topic, user pool and the ingestion bucket (read-only use of `processed/documents/*`) as constructor props.

**Knowledge bucket**

- Name: `school-knowledge-docs-<account>-<region>` (built from tokens). SSE-S3, `enforce_ssl=True`, block all public access. Versioning off: every object's key already includes the document version, and objects are never overwritten.
- Prefixes:
  - `sources/<school_id>/<document_id>/<version>/source.<ext>`: this service's copy of the document, taken from the ingestion staging area when the version is indexed. It is what re-indexing reads.
  - `vector-keys/<school_id>/<document_id>/<version>.json`: the JSON list of a version's vector keys (section 5, step 8).
- Retention follows the registry, not a clock: a version's source and key list are deleted when the version is superseded, retired or permanently fails (section 5). Only active versions keep files. Lifecycle: abort incomplete multipart uploads after 1 day; no age-based expiry (an active policy may stay live for years).

**S3 Vectors**

- One vector bucket `school-knowledge-vectors-<account>-<region>` (build the name from tokens).
- One vector index named per section 3, `float32`, cosine distance, dimension from config.
- Filterable metadata: `school_id`, `document_id`, `version`, `audience` (list of strings).
- Non-filterable metadata: `chunk_text`, `title`, `page_start`, `page_end`, `heading_path`. Declaring these non-filterable at index creation keeps them out of the filter index; check the current S3 Vectors per-vector metadata size limits and size chunks so they fit. If a chunk would exceed the limit, fail fast in tests rather than truncating silently.
- Define these with the CloudFormation resource types for S3 Vectors through CDK L1 constructs. Verify the exact construct and property names against the installed `aws-cdk-lib` version; if they are not available, use a custom resource and note why in the README.

**DynamoDB `DocumentRegistry`**

| Item | PK | SK | Attributes |
|---|---|---|---|
| Document | `SCHOOL#<school_id>#DOC#<document_id>` | `META` | `title`, `audience`, `active_version`, `status` (`INDEXING`, `ACTIVE`, `FAILED`, `RETIRED`), `updated_at` |
| Version | same | `VER#<version>` | `status`, `chunk_count`, `source_key` (knowledge bucket key of the source copy), `vector_keys_key` (knowledge bucket key of the JSON list of this version's vector keys), `embedding_model`, `index_name`, `indexed_at`, `import_id` |

GSI `BySchoolStatus`: PK `school_id`, SK `status#updated_at`, for the admin listing.

**DynamoDB `KnowledgeIdempotency`:** Powertools idempotency table.

**Indexing pipeline**

- EventBridge rule on `SchoolEventBus`: `source = school.ingestion`, `detail-type = DocumentAccepted` → SQS `document-indexing-queue` (DLQ `document-indexing-dlq`, `maxReceiveCount: 3`, retention 14 days). Both queues use SQS-managed encryption and `enforce_ssl=True`. The `admin_ops` Lambda also sends internal re-index messages to this queue (section 8).
- Indexer Lambda: 1536 MB, 600 s timeout; queue visibility timeout 3600 s (6×); batch size 1; `report_batch_item_failures`; event source `max_concurrency` 2. The low concurrency deliberately keeps embedding calls inside provider rate limits.
- Read access to `processed/documents/*` in the ingestion bucket only; never any other prefix of that bucket.

**Query API**

- HTTP API with a Cognito JWT authorizer, default route throttling (burst 20, rate 10 per second) and CORS limited to a configured origin list.
- Query Lambda: 1024 MB, 25 s timeout (under API Gateway's integration limit), provisioned concurrency **off** (cost); note cold-start trade-off in README.
- Routes:
  - `POST /ask`: `staff`, `parent`, `admin`.
  - `POST /feedback`: `staff`, `parent`, `admin`.
  - `GET /documents`: `admin` (lists registry entries for the caller's school).
  - `POST /documents/{documentId}/retire`: `admin`.

**Alarms → ops topic**

- Indexing DLQ visible messages > 0.
- Query Lambda p95 duration > 15 s over 15 minutes.
- `ProviderErrorCount` > 5 in 5 minutes.
- `EmbeddingModelMismatch` > 0.

**IAM (least privilege)**

Rules, as in the ingestion service: no statement may have an `Action` of `*` (so no `s3vectors:*`); resource wildcards only as prefix scopes on this service's own resources (and the ingestion bucket's `processed/documents/*`) or where AWS requires `*` (X-Ray); no AWS managed policies (write CloudWatch Logs permissions per function).

- Indexer: `s3:GetObject` on `<ingestion-bucket>/processed/documents/*` (the source of a cross-bucket `copy_object`); `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `<knowledge-bucket>/sources/*` and `<knowledge-bucket>/vector-keys/*`; S3 Vectors put, get and delete on the one index ARN; read/write on `DocumentRegistry` and `KnowledgeIdempotency`; `secretsmanager:GetSecretValue` on the embedding provider's secret; `events:PutEvents` on the bus.
- Query API: S3 Vectors query and get on the index ARN; `dynamodb:BatchGetItem`/`GetItem`/`Query`/`UpdateItem` on `DocumentRegistry`; read/write on `QueryLog`; `GetSecretValue` on both the embedding and generation secrets; `events:PutEvents` on the bus. The retire route additionally needs S3 Vectors delete on the index ARN and `s3:GetObject`/`s3:DeleteObject` on `<knowledge-bucket>/sources/*` and `vector-keys/*`.
- `admin_ops`: `dynamodb:Query`/`GetItem` on `DocumentRegistry` and its GSI; `sqs:SendMessage` on `document-indexing-queue`; the DLQ inspect/redrive permissions listed for the ingestion service's replay role.
- Check the exact S3 Vectors IAM action names in AWS documentation.

**cdk-nag:** fix what can be fixed (SSL enforcement on buckets, queues and the topic). Expected suppressions, at the narrowest scope with a written reason: `AwsSolutions-IAM5` for the prefix scopes and X-Ray; `AwsSolutions-S1` for both buckets (same reasoning as ingestion); Cognito findings that conflict with the stated design (e.g. MFA optional rather than required), with the reason. Any other suppression must be raised with me first.

## 5. Indexing behaviour (`src/knowledge/handlers/indexer.py`)

For each `DocumentAccepted` event:

Messages arrive in two shapes: a `DocumentAccepted` event delivered by the EventBridge rule, or an internal re-index message from `admin_ops` (section 8). Parse both into one `IndexJob` model; the steps below say where they differ.

1. **Idempotency** keyed on `document_id + version + index_name` (the index name matters: re-indexing the same version into a new index is new work). Use Powertools `idempotent_function` with `config.register_lambda_context(context)` so an in-progress record expires with the invocation. If that version is already `ACTIVE` **in the configured index**, return success.
2. **Registry:** set the version item to `INDEXING`.
3. **Take ownership of the source, then download it.**
   - For `DocumentAccepted`: server-side `copy_object` from the event's `stagedKey` in the ingestion bucket to `sources/<school_id>/<document_id>/<version>/source.<ext>` in the knowledge bucket, and record `source_key` on the version item. If the knowledge copy already exists (a retry after a previous attempt copied it), skip the copy.
   - For a re-index message: the source is already in the knowledge bucket; use its `source_key`.
   - Download the knowledge copy to `/tmp` (max 25 MB, enforced by ingestion; assert again) and compute its SHA-256. For `DocumentAccepted`, it must equal the event's `sha256`, and `version` must equal `"v-" + sha256[:12]`; a mismatch is a permanent `EXTRACTION_FAILED` (log `reason=hash_mismatch`).
   - **Staged copy gone.** Ingestion deletes staged copies after 45 days, so a very old event (e.g. a DLQ redrive long after upload) can find nothing at `stagedKey`. If the knowledge copy exists, continue from it; otherwise this is a permanent `EXTRACTION_FAILED` (log `reason=source_unavailable`), and the admin re-uploads the document.
4. **Extract** text, preserving structure:
   - PDF (`pypdf`): text per page, keeping page numbers. If extracted text averages under 50 characters per page, fail with `NO_EXTRACTABLE_TEXT` (probably scanned).
   - DOCX (`python-docx`): paragraphs with heading levels from paragraph styles; tables flattened row by row as `cell | cell | cell`.
   - Markdown: split on `#` headings. Plain text: paragraphs.
   - Normalise whitespace, remove repeated page headers/footers (lines that appear on more than half the pages), and fix hyphenated line breaks.
5. **Chunk:**
   - Prefer structural boundaries (headings, then paragraphs); never split mid-sentence when avoidable.
   - Target 500 tokens, hard maximum 800, overlap 75 tokens. Estimate tokens as `len(text) / 4` (no tokenizer dependency), and keep the estimate within the embedder's `max_input_tokens`.
   - Prefix each chunk's embedded text with its context: `"<title> > <heading path>\n\n<chunk text>"`. Store the plain chunk text separately for display.
   - Record `page_start`/`page_end` (PDF) and `heading_path` for citations.
   - Drop chunks under 20 tokens unless they are the only chunk.
6. **Embed** in batches of `embedder.max_batch_size` using the document task type. Retry per section 3.
7. **Write vectors** in batches (check the current per-request S3 Vectors limit and make it configurable). Vector key: `<document_id>#<version>#<chunk_no:05d>`. Metadata per section 4.
8. **Record vector keys** for this version as a JSON list at `vector-keys/<school_id>/<document_id>/<version>.json` in the knowledge bucket, and its key in the version item. This is how a version's vectors are found later for deletion, since vector stores are not designed to be listed by metadata.
9. **Swap versions:** in one `TransactWriteItems`, set `META.active_version` to the new version and the new version item to `ACTIVE`, conditioned on `META.active_version` still equalling the value read in step 2 (protects against two versions finishing out of order; if the condition fails, re-read and only activate if this version is newer by `indexed_at`).
10. **Delete the previous version's vectors** using its key list, then delete its key list and its source copy from the knowledge bucket, and mark it `SUPERSEDED`. New vectors are written before old ones are deleted, so a query never finds a document missing mid-update. The query path filters out non-active versions during the brief overlap (section 6, step 5).
11. **Publish** `DocumentIndexed` with `replacedVersion`. For a re-index message, skip steps 9–11 (the version is already active and nothing is replaced): update the version item's `index_name`, `embedding_model`, `chunk_count` and `vector_keys_key`, delete the old key list, emit metrics, and publish nothing, because the document was already live and notifications must not tell the uploader again. Vectors in the old index are removed when the old index is deleted.

On failure: set the version to `FAILED`, delete any vectors already written for this version (from the partial key list), publish `DocumentIndexingFailed` with the right reason code. Raise for retryable errors (provider 5xx/429 after retries, throttling) so SQS retries, and keep the source copy so the retry can reuse it; return success for permanent ones (`NO_EXTRACTABLE_TEXT`, `EXTRACTION_FAILED`) and delete this version's source copy and key list. A failed re-index never changes the active version or deletes its source.

**Retire** (`POST /documents/{id}/retire`): delete the active version's vectors, then its key list and source copy from the knowledge bucket, set status `RETIRED`, publish `DocumentRetired`. Re-enabling a retired document means re-uploading it through ingestion.

## 6. Query behaviour (`src/knowledge/handlers/api.py`, Powertools `APIGatewayHttpResolver`)

`POST /ask` body: `{ "question": str, "topK": int (optional, 1–10, default 6) }`.

1. **Identity from the JWT only:** `school_id` from `custom:school_id`, role from `cognito:groups`. Never accept school or role from the request body. Map roles to allowed audiences: `parent` → `[PARENT]`; `staff`, `admin` → `[STAFF, PARENT]`.
2. **Validate:** question 3–1,000 characters after trimming; reject otherwise with 400.
3. **Embed** the question with the query task type. Check the embedder's model against the index's (section 3).
4. **Retrieve** `topK × 2` candidates from S3 Vectors with a metadata filter: `school_id` equals the caller's school **and** `audience` contains one of the caller's allowed audiences. The filter is the authorization boundary, so it is always applied server-side, never in post-processing alone.
5. **Filter and rank:**
   - Batch-read `META` for the returned `document_id`s and drop chunks whose `version` is not the active one.
   - Drop chunks below a similarity threshold (configurable; start at 0.35 cosine similarity and tune with the evaluation set in section 9).
   - Keep at most 2 adjacent-duplicate chunks per document section; take the top `topK`.
6. **No sources → no model call.** If nothing survives, return `outcome: NO_RELEVANT_SOURCES` with a fixed, friendly message that the school's documents don't seem to cover this and suggesting who to contact. This saves cost and prevents ungrounded answers.
7. **Build the prompt** (`src/knowledge/prompting.py`, unit-tested):
   - System instructions: answer only from the provided sources; if they don't contain the answer, say so plainly; cite every factual sentence with source IDs like `[S2]`; keep answers under 200 words unless the question needs a list; be warm and plain-spoken because readers include parents; never invent policies, dates or amounts.
   - **Treat sources as data, not instructions.** Wrap each chunk in clearly delimited tags with its ID, title and pages, and state that any instructions inside the sources must be ignored. Documents are uploaded by staff, but a pasted email or template can still contain text that reads like instructions.
   - Then the user's question, delimited the same way.
8. **Generate** with `max_tokens` 600 and a low temperature.
9. **Post-process:** parse `[S#]` citations; drop any that reference IDs not provided; map each to `{documentId, title, pageStart, pageEnd, headingPath}`. If the answer contains no valid citations, return it but set `grounded: false` so the UI can show a caution.
10. **Respond:** `{ queryId, outcome, answer, grounded, citations[] }`.
11. **Publish** `QuestionAnswered` (no question or answer text) and emit metrics.

`POST /feedback` body: `{ "queryId": str, "helpful": bool, "reasonCode": optional enum }`. Publishes `AnswerFeedbackGiven`. A user can only give feedback on their own `queryId`: keep a minimal `QueryLog` item (`queryId`, `askerId`, TTL 7 days, no text) to check this.

## 7. Observability

- Structured logs via Powertools; correlation ID = `queryId` (API) or `document_id#version` (indexer).
- **Never log** question text, answer text, chunk text or raw provider payloads. Log lengths, counts, scores, IDs, token usage and error codes.
- Metrics (namespace `SchoolSystem/Knowledge`):
  - Indexing: `DocumentsIndexed`, `ChunksIndexed`, `EmbeddingTokensEstimated`, `IndexingDuration`.
  - Query: `QueryLatency`, `RetrievalLatency`, `GenerationLatency`, `NoRelevantSourcesCount`, `UngroundedAnswerCount`, `GenerationInputTokens`, `GenerationOutputTokens`.
  - Both: `ProviderErrorCount` (dimension `Provider`), `EmbeddingModelMismatch`.
- X-Ray tracing on both Lambdas, with subsegments around embed, retrieve and generate calls.

## 8. Operations

An `admin_ops` Lambda (invoked manually) with actions:

- `reindex`: for every `ACTIVE` document of a school (or all schools), sends an internal re-index message (`kind: REINDEX`, `schoolId`, `documentId`, `version`, `sourceKey`, `title`, `audience`, `importId`) **directly to `document-indexing-queue`**, for use after creating a new index for a new embedding model. Throttled to a configurable rate. It does not publish a synthetic `DocumentAccepted` to the bus, for two reasons: only the ingestion service may publish events with `source = school.ingestion`, and the staged copy a `DocumentAccepted` points to has usually expired by then. A re-index reads from this service's own source copy instead.
- `inspect_dlq` and `redrive_dlq`: same pattern as the ingestion service's `dlq_replay`.

## 9. Evaluation (a required deliverable, not optional)

Retrieval quality is the thing most RAG demos never measure. Build `eval/`:

- `eval/corpus/`: 5–8 realistic but **fictional** school documents you write (code of conduct, fee policy, uniform policy, assessment policy, transport rules), as PDF, DOCX and Markdown.
- `eval/questions.yaml`: at least 30 questions, each with the expected document ID(s) and section, an expected key fact, and a `role`. Include:
  - Questions answerable from exactly one section.
  - Questions needing two documents.
  - Questions the corpus cannot answer (expected outcome `NO_RELEVANT_SOURCES`).
  - Staff-only questions asked as a parent (must not retrieve staff-only content).
  - A document containing an embedded instruction ("ignore previous instructions and…") that must not change behaviour.
- `eval/run_eval.py`: runs against a deployed dev stage and reports retrieval hit rate @ k, mean reciprocal rank, correct-refusal rate, audience-leak count (must be 0), injection-resistance pass rate, average latency and cost per question. It also writes a Markdown report.
- Run it with at least two provider configurations (e.g. Gemini embeddings + Claude generation, and Gemini for both) and put the comparison table in the README.

## 10. Testing and CI

**Unit tests (`FakeEmbedder`, `FakeGenerator`, and an `InMemoryVectorStore` implementing the same `VectorStore` interface as the S3 Vectors adapter):**

- Extraction: page numbers preserved; repeated headers removed; DOCX headings and tables handled; scanned-PDF detection.
- Chunking: size bounds, overlap, no mid-sentence splits in normal prose, context prefix, metadata.
- Indexing: first upload copies the source into the knowledge bucket and records `source_key`; re-upload swaps versions and deletes the old version's vectors, key list and source copy; out-of-order completion handled; failure cleans up partial vectors (and, for permanent failures, the source copy); idempotent redelivery.
- Source handling: a SHA-256 mismatch against the event fails with `EXTRACTION_FAILED`; a missing staged copy with an existing knowledge copy succeeds; a missing staged copy with no knowledge copy fails with `EXTRACTION_FAILED`.
- Re-index: a `REINDEX` message reads from the knowledge copy, writes to the configured index, updates the version item, publishes no event, and is idempotent per index (the same version re-indexed into a second index is processed again).
- Retire deletes vectors, key list and source copy.
- Query: audience filter enforced (parent never sees staff-only chunks); school isolation; inactive versions dropped; threshold and no-sources path skips the generator; citation parsing drops invented IDs; `grounded` flag.
- Prompt builder: sources delimited and labelled; injection notice present.
- Providers: retry and backoff on 429/5xx (stubbed HTTP); dimension assertion; model mismatch detection.
- Feedback ownership check.

**Infrastructure tests (CDK `assertions`):** index dimension and distance metric from config; knowledge bucket encryption, SSL enforcement and public access block; the indexer's only grant on the ingestion bucket is `s3:GetObject` on `processed/documents/*`; no AWS managed policies; JWT authorizer on every route; admin-only routes; queue timeouts and DLQ; no IAM wildcard actions; secrets not exposed as plain environment variables; `cdk-nag` passes with written reasons for any suppression.

**Integration (opt-in, `pytest -m integration`):** deploy to dev, upload one evaluation document through the ingestion bucket with the metadata ingestion requires (`--metadata uploaded-by=USR-admin-0001,audience=PARENT,title=...`), wait for `DocumentIndexed`, ask one question as a parent test user, assert a citation to that document.

**CI:** `ruff`, `mypy`, unit and infra tests, the arm64 dependency layer build, and `cdk synth` on every push. Evaluation runs manually (it costs money and needs a deployed stage).

## 11. Instructions to the coding agent

1. Work in phases and stop for review after each: (a) `SharedStack` Cognito additions, `KnowledgeStack` infrastructure (including the knowledge bucket and the arm64 dependency layer), infra tests; (b) provider interfaces, fakes and real adapters with unit tests; (c) extraction and chunking; (d) indexer end to end with the in-memory store, then the S3 Vectors adapter; (e) query API; (f) admin operations, alarms, metrics; (g) evaluation harness and README.
2. Run the full test suite and `cdk synth` at the end of every phase.
3. Before writing any provider adapter, read that provider's current API documentation and state the model names, dimensions and limits you will use. Do not rely on memory for these.
4. Do not hard-code account IDs, regions, bucket names, model names or API keys.
5. If this spec is ambiguous or seems wrong, say so and propose a resolution before implementing. Do not silently deviate.
6. Keep handlers thin; put logic in importable, typed modules.

## 12. Out of scope for v1

- OCR for scanned PDFs (Amazon Textract, phase 2).
- Hybrid keyword + vector search and re-ranking (phase 2: add a re-ranking step behind its own interface and measure it with the evaluation set).
- Streaming answers to the client.
- Conversation memory (each question is independent).
- Answering from personal data (marks, balances). That would require per-record authorization and a different privacy design.

## 13. README contents

- Architecture diagram (Mermaid): indexing path and query path, with the bus, vector index, registry and providers.
- How this compares with Bedrock Knowledge Bases, and what the provider interface would need to add a Bedrock implementation.
- Setup: creating provider API keys, storing them in Secrets Manager, deploying, creating test users in each Cognito group.
- Example requests: get a token for the test client, then call `/ask` with `curl`.
- Evaluation results table and what changed between runs.
- Design decisions: why chunk context prefixes; why the audience filter lives in the vector query; why no model call without sources; why write-then-delete version swaps; why model and index are bound; why questions are never logged; why the service keeps its own copy of each source (ingestion's staging expires after 45 days, and re-indexing needs the original); why re-indexing uses an internal queue message rather than a synthetic `DocumentAccepted`.
- Cost notes (embedding, generation, S3 Vectors, API Gateway) for 100 documents and 1,000 questions a month; teardown steps.

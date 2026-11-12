# Build Prompt: Serverless Bulk File Ingestion Service

> **How to use this file:** give it to a coding agent (e.g. Claude Code) as the task specification, together with `EVENT_CATALOGUE.md`. Section 9 tells the agent how to work.

## 1. Context

You are building the **file ingestion service** for a multi-tenant school management system on AWS. Other services (student records, fee ledger, gradebook, knowledge, notifications, analytics) communicate only through a shared EventBridge bus, as defined in `EVENT_CATALOGUE.md`.

This service's single job: accept bulk CSV and JSON uploads (and policy documents) from school staff, validate them, stage the valid output, write an audit report, and announce the result on the bus. **It is not the source of truth for any domain data.** Domain services consume its validated output through the claim-check files referenced in its events and own the real records. All staged data therefore expires automatically.

The `IngestedRecords` DynamoDB table (section 4) is **internal to this service**. No other service reads it or is granted access to it; consumers use `valid.jsonl`.

## 2. Technical stack (fixed)

- **IaC:** AWS CDK v2 in **Python**, using modular L3-style constructs.
- **Runtime:** the newest Python Lambda runtime that both AWS Lambda and Powertools for AWS Lambda (Python) support at build time (3.13 or later), `arm64` architecture.
- **Libraries:** Powertools for AWS Lambda (logging, metrics, tracing, batch processing, idempotency, parser), `pydantic` v2 for record schemas, `ijson` for streaming JSON parsing, `python-ulid` for event IDs, `boto3`.
- **Dependency packaging:** `pydantic-core` and `ijson` (C backend) are compiled. Build one dependencies layer containing Powertools, pydantic, ijson and python-ulid with `pip install --platform manylinux2014_aarch64 --implementation cp --python-version <runtime version> --only-binary=:all: --target <dir>`, so it works on an x86 laptop and on x86 CI runners without Docker. Do not use the public Powertools layer as well (two copies of pydantic on the path cause version clashes). `boto3` comes from the runtime.
- **Storage:** S3, DynamoDB (on-demand billing).
- **Messaging:** SQS standard queue with DLQ; EventBridge custom bus.
- **Quality:** `pytest`, `moto`, CDK `assertions`, `cdk-nag` (AwsSolutions pack), `ruff`, `mypy --strict` on `src/`.

## 3. Upload contract

Files are uploaded to:

```
s3://<bucket>/raw/<dataset_type>/<school_id>/<filename>.<csv|json|jsonl>
```

- `dataset_type` is one of `roster`, `marks`, `fee_schedule`, `documents`. Any other value → `ImportFailed` with `reasonCode: UNSUPPORTED_FORMAT`.
- The path segment is lowercase; the `datasetType` value in events and the `<DATASET>` part of DynamoDB keys is its uppercase form (`marks` → `MARKS`, `fee_schedule` → `FEE_SCHEDULE`). Implement this mapping once, in one module.
- For `documents`, the key may contain subfolders after `school_id` (e.g. `raw/documents/SCH-001/policies/code_of_conduct.pdf`), and the allowed extensions are `.pdf`, `.docx`, `.md`, `.txt` instead of `.csv`, `.json`, `.jsonl`. A document extension under a tabular dataset, or a tabular extension under `documents`, → `UNSUPPORTED_FORMAT`.
- `school_id` matches `^SCH-\d{3}$`.
- **Required metadata for every dataset:** the uploader's user ID as S3 object metadata `x-amz-meta-uploaded-by`. Missing or empty → `ImportFailed` with `MISSING_METADATA`. In v1 this is set by the CLI upload command; later it will come from a presigned upload URL issued by the authenticated API.
- **Maximum file sizes for v1:** tabular datasets **20 MB** (`MAX_TABULAR_BYTES`), documents **25 MB** (`MAX_DOCUMENT_BYTES`). Larger files → `ImportFailed` with `FILE_TOO_LARGE`. A real school roster or marks file is well under 1 MB; the 20 MB cap keeps a worst-case file comfortably inside the 300 s worker timeout, including the DynamoDB writes. (See section 8 for the phase 2 approach to larger files.)
- **Bucket versioning is required.** The worker identifies files by `versionId`, not ETag.

### Dataset schemas (pydantic models in `src/ingestion/schemas/`)

All tabular models use `model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)`. For CSV, unknown columns are a file-level `HEADER_MISMATCH`; for JSON and JSONL, an unknown field on a record is a row-level validation error (`errorCode: extra_forbidden`), and the other rows are still processed.

**roster**

| Column | Type | Rules |
|---|---|---|
| `student_number` | str | `^STU-\d{4}-\d{5}$`, natural key |
| `first_name`, `last_name` | str | 1–60 chars, trimmed |
| `date_of_birth` | date | ISO `YYYY-MM-DD`, age 4–21 at upload time |
| `grade_code` | str | `R` or `1`–`12` |
| `class_code` | str | `^(R\|[1-9]\|1[0-2])[A-Z]$` and consistent with `grade_code` |
| `guardian_email` | str | valid email |
| `guardian_phone` | str | E.164, e.g. `+27821234567` |

**marks**

| Column | Type | Rules |
|---|---|---|
| `student_number` | str | as above |
| `assessment_code` | str | 1–40 chars |
| `term_code` | str | `^\d{4}-T[1-4]$` |
| `mark` | Decimal | `0 ≤ mark ≤ max_mark` |
| `max_mark` | Decimal | `> 0`, ≤ 1000 |

Natural key: `student_number` + `assessment_code` + `term_code`.

**fee_schedule**

| Column | Type | Rules |
|---|---|---|
| `grade_code` | str | as above |
| `term_code` | str | as above |
| `fee_item` | str | 1–60 chars |
| `amount_cents` | int | `> 0`; reject decimals |
| `currency` | str | must be `ZAR` in v1 |

Natural key: `grade_code` + `term_code` + `fee_item`.

**documents**

Not row data: no parsing or row validation here. The knowledge service extracts and indexes the text.

| Requirement | Rule |
|---|---|
| Max size | 25 MB |
| `x-amz-meta-title` | optional, 1–120 chars; defaults to the file name without extension |
| `x-amz-meta-audience` | required; comma-separated subset of `STAFF`, `PARENT`; missing or invalid → `ImportFailed` with `MISSING_METADATA` |
| Content check | `.pdf`: starts with `%PDF`. `.docx`: a valid ZIP archive that contains a `word/document.xml` entry (a ZIP header alone also matches `.xlsx` and `.zip` files). `.md`/`.txt`: decodes as UTF-8. Mismatch → `UNSUPPORTED_FORMAT` |

### Formats

- **CSV:** UTF-8 (strip BOM), header row required. Header must contain exactly the schema's columns in any order; missing or unknown columns → `ImportFailed` with `HEADER_MISMATCH`.
- **JSON:** a top-level array of objects, parsed incrementally with `ijson` (never `json.load` on the whole body).
- **JSONL:** one object per line; blank lines are skipped.
- A body that is not valid UTF-8, or JSON/JSONL that is malformed (as opposed to well-formed but failing validation), → `ImportFailed` with `UNREADABLE`, wherever in the file the problem appears. See section 5, step 4.
- A file with no data rows, or in which every row fails validation, → `ImportFailed` with `NO_VALID_ROWS` (the summary still lists the errors).

## 4. Infrastructure (`infra/`)

Two stacks:

**`SharedStack`** creates `SchoolEventBus` (custom bus), an archive on it (30-day retention), and an SNS topic `ops-alerts` with `enforce_ssl=True`. Later services will reuse these. It exports them as stack properties, not CloudFormation exports by name.

**`IngestionStack`** receives the bus and topic as constructor props and creates the following.

### S3 bucket

- Name: `school-ingestion-<account>-<region>`, built from `Stack.of(self).account` / `region` tokens, never hard-coded.
- SSE-S3 encryption, `enforce_ssl=True`, block all public access, **versioning on** (required; see section 3).
- Lifecycle (retention is set by data protection, not storage cost: `raw/` and `processed/` hold minors' personal data, and staged data must not outlive its purpose):
  - `raw/` → expire after **30 days**. No Standard-IA transition: IA has a 30-day minimum storage charge, so transitioning objects that expire at 30 days saves nothing.
  - `processed/` → expire after **45 days**. This must exceed the 30-day event archive retention, so a replayed `ImportCompleted` or `DocumentAccepted` still finds its claim-check files.
  - Expire noncurrent versions after 30 days; abort incomplete multipart uploads after 1 day.
  - Do **not** use Glacier classes: their 90-day minimum storage charge makes them uneconomic at these retention periods.
  - Note in the README that lifecycle expiry is asynchronous: objects can remain a day or two past their expiry date.
- Event notifications to the main queue on `s3:ObjectCreated:*`. S3 allows one prefix and one suffix per filter, so register **seven** notifications, generated in a loop: prefix `raw/` with suffixes `.csv`, `.json`, `.jsonl`, `.pdf`, `.docx`, `.md`, `.txt`. The worker rejects a document extension under a tabular dataset and vice versa.

### SQS

- `file-processing-dlq`: retention 14 days, SQS-managed encryption, `enforce_ssl=True`.
- `file-processing-queue`: SQS-managed encryption, `enforce_ssl=True`, visibility timeout **6 × worker timeout** (worker timeout 300 s → visibility 1800 s), redrive to the DLQ with `maxReceiveCount: 3`.
- Use SQS-managed encryption deliberately: a customer-managed KMS key would need extra key-policy grants for S3 to publish, a common silent failure.
- **Queue policy:** allow `s3.amazonaws.com` `sqs:SendMessage` only with conditions on **both** `aws:SourceArn` (the bucket ARN, which contains no account ID) and `aws:SourceAccount`. CDK's built-in `SqsDestination` adds only an `aws:SourceArn` condition (check this against the CDK version you install). Because policy statements are additive, adding a second, stricter statement alongside it does not help. Instead, implement a small `IBucketNotificationDestination` in `infra/constructs/processing_queue.py` whose `bind()` adds a single statement with both conditions to the queue's resource policy and returns the queue's ARN with the queue policy as a dependency, so the bucket notification is created only after the policy exists. Test the resulting policy (section 7).

### DynamoDB

**`IngestedRecords`** (internal staging table; no other service is granted access)

- `PK` (S): `SCHOOL#<school_id>#<DATASET>#<natural_key>`
- `SK` (S): `IMPORT#<import_id>`
- Attributes: the validated record fields, plus `ingested_at`, `source_file_key`, `source_version_id`, `import_id`, `record_hash` (SHA-256 of the canonical JSON of the validated record), `ttl_timestamp` (epoch seconds, `ingested_at` + 30 days).
- TTL enabled on `ttl_timestamp`; point-in-time recovery on.
- GSI `ByImport`: PK `import_id`, SK `PK`. Used by this service's own ops tooling and tests to inspect one import; not a consumer interface.
- Purpose, for the README: keeps a per-key history across imports (useful for spotting conflicting re-uploads via `record_hash`) and demonstrates batched, retried writes. It is not required by any consumer.

**`IngestionIdempotency`**: the table required by Powertools idempotency (`id` partition key, TTL on `expiration`).

### Worker Lambda (`etl_worker`)

- 1024 MB memory, 300 s timeout, `arm64`, dependencies layer (section 2), X-Ray active tracing.
- SQS event source: batch size 1 (one file per invocation), `report_batch_item_failures=True`, and **`max_concurrency=5`** on the event source mapping. Use the event source mapping's maximum concurrency for backpressure, **not** reserved concurrency: reserved concurrency throttles the poller, and throttled messages burn receive attempts and can land in the DLQ without ever failing.
- Environment: table names, bus name, bucket name, `MAX_TABULAR_BYTES` (20971520), `MAX_DOCUMENT_BYTES` (26214400), `POWERTOOLS_SERVICE_NAME=ingestion`, `POWERTOOLS_METRICS_NAMESPACE=SchoolSystem/Ingestion`.

### DLQ replay Lambda (`dlq_replay`)

Invoked manually (console or CLI) with one of:

- `{"action": "inspect", "max": 10}`: receives up to `max` DLQ messages **without deleting them** (short visibility timeout), returns bucket, key, versionId, ETag, receive count and the first-failure timestamp.
- `{"action": "redrive", "maxPerSecond": 10}`: calls `sqs:StartMessageMoveTask` from the DLQ back to the main queue and returns the task handle.
- `{"action": "status", "taskHandle": "..."}`: calls `ListMessageMoveTasks`.

### Alarms → `ops-alerts` topic

- DLQ `ApproximateNumberOfMessagesVisible > 0` for 1 datapoint of 1 minute.
- Main queue `ApproximateAgeOfOldestMessage > 900` s.
- Worker `Errors > 0` over 5 minutes.

### IAM (least privilege)

Rules: no statement may have an `Action` of `*`. Resource wildcards are allowed only as **prefix scopes** on this service's own resources (e.g. `<bucket>/raw/*`) and where AWS requires `*` (X-Ray `PutTraceSegments` / `PutTelemetryRecords`). Use inline or customer-managed policies rather than AWS managed policies (e.g. write the CloudWatch Logs permissions yourself instead of attaching `AWSLambdaBasicExecutionRole`).

Worker role:

- `s3:GetObject`, `s3:GetObjectVersion` on `arn:…:<bucket>/raw/*` (`GetObjectVersion` is required because every read and copy names a `VersionId`)
- `s3:PutObject`, `s3:AbortMultipartUpload` on `arn:…:<bucket>/processed/*` (`PutObject` covers create/upload-part/complete for multipart uploads; abort is a separate action)
- `dynamodb:BatchWriteItem` on `IngestedRecords`
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` on `IngestionIdempotency`
- `events:PutEvents` on `SchoolEventBus`
- CloudWatch Logs on the function's own log group; X-Ray as above
- SQS consume permissions (granted by the event source mapping)

Replay role:

- `sqs:ReceiveMessage`, `ChangeMessageVisibility`, `GetQueueAttributes` on the DLQ
- `sqs:StartMessageMoveTask`, `ListMessageMoveTasks` on the DLQ
- `sqs:SendMessage` on the main queue
- CloudWatch Logs on its own log group

### cdk-nag

Fix every finding that can reasonably be fixed (e.g. SSL enforcement on the topic and queues). The following findings are expected and should be suppressed **at the narrowest scope possible, each with a written reason**:

- `AwsSolutions-IAM5`: the `raw/*` and `processed/*` prefix scopes, and the X-Ray `*` resource.
- `AwsSolutions-S1` (no server access logging): a portfolio-scale project; an access-log bucket would itself need logging. Mention CloudTrail S3 data events as the production alternative in the README.
- `AwsSolutions-L1` only if the newest runtime isn't yet supported by Powertools at build time.

Any other suppression must be raised with me before it is added.

## 5. Worker behaviour (`src/ingestion/handlers/etl_worker.py`)

Use the Powertools `BatchProcessor` for SQS. For each message:

1. **Parse the envelope.** The SQS body is an S3 event notification JSON. Ignore `s3:TestEvent` messages (S3 sends one when the notification is created) by returning success. Extract bucket, key (URL-decode it: S3 encodes spaces as `+`), `versionId`, size, ETag, `eventTime`. A record without `versionId` means versioning is off: raise (it is a deployment fault, not a bad file).

2. **Route and pre-check.** Parse `dataset_type`, `school_id` and the extension from the key, then `head_object` with the `VersionId` to read metadata. In this order, fail fast with:
   - unknown `dataset_type`, extension that doesn't belong to the dataset, or invalid `school_id` → `UNSUPPORTED_FORMAT`
   - size above the dataset's limit → `FILE_TOO_LARGE`
   - missing `uploaded-by` (and, for documents, missing or invalid `audience`) → `MISSING_METADATA`
   - `head_object` returns 404 (the version was deleted before processing) → `UNREADABLE`

   For each: write a summary, publish `ImportFailed`, and return **success** (a bad file is a business outcome, not a retryable error; retrying it three times only fills the DLQ).

   **Where rejected files are reported.** When `dataset_type` and `school_id` are both valid, the summary goes to `processed/<dataset_type>/<school_id>/<import_id>/summary.json`. Otherwise the key components can't be trusted to build a path, so use `processed/_rejected/<import_id>/summary.json`, and in the event set `datasetType` and/or `metadata.schoolId` to `UNKNOWN` (per `EVENT_CATALOGUE.md`). Never echo an unvalidated key component into a new S3 key.

3. **Idempotency.** `import_id = "imp-" + sha256(f"{bucket}/{key}/{version_id}")[:12]`. Keying on `versionId` (not ETag) means a redelivered message for the same object version is not reprocessed, while a deliberate re-upload of an identical file is a new version and **is** processed. Wrap the per-file processing in Powertools `idempotent_function` keyed on `import_id`, call `config.register_lambda_context(context)` so an in-progress record expires with the invocation, and set `expires_after_seconds` to 24 hours. The function returns a small JSON-serialisable result (status and counts), not the records.

4. **Stream and validate.** `get_object` with the `VersionId` and read the `StreamingBody` incrementally: `codecs.iterdecode` over `iter_lines()` fed into `csv.DictReader` for CSV, `ijson.items(body, "item")` for JSON, line by line for JSONL. Validate each record with the dataset's pydantic model. Track the 1-based line number (data rows; header is line 1 for CSV; for JSON use the 1-based array index and name it `line` for consistency).

   **Parse failures mid-file** (`UnicodeDecodeError`, `ijson.JSONError`, a JSONL line that isn't JSON): stop reading, abort the multipart upload (step 6), write the summary with counts so far, publish `ImportFailed` with `UNREADABLE`, and return success. Rows already written to DynamoDB stay until TTL removes them; no `ImportCompleted` is published, so no consumer reads them.

5. **Write in batches.** Buffer valid items; flush 25 at a time via `batch_write_item`. Retry `UnprocessedItems` with exponential backoff and jitter (max 5 attempts); if items remain unprocessed, raise, so the message is retried. Note in code comments that `batch_write_item` reduces request count, not write capacity units, and has no condition expressions; idempotency is therefore handled at file level (step 3), and rewriting the same item with the same `SK` is naturally idempotent.

6. **Claim-check output.** Stream valid records as JSON lines to `processed/<dataset_type>/<school_id>/<import_id>/valid.jsonl` without accumulating the file in memory, via a writer class in `src/ingestion/storage/` that:
   - buffers until it has at least 5 MiB, then uploads a part (S3 requires every part except the last to be ≥ 5 MiB);
   - creates the multipart upload lazily, on the first full part;
   - on close: if nothing was written at all, writes no object (step 8 handles zero valid rows); if no part was uploaded yet, writes the buffer with a single `put_object`; otherwise uploads the remainder as the last part and completes;
   - on any exception, calls `abort_multipart_upload` (used as a context manager so this can't be forgotten). The 1-day lifecycle rule is the backstop.

7. **Error report.** Collect validation errors as `{ "line": int, "field": str, "errorCode": str, "message": str, "value": str }`, with the value truncated to 100 chars. Keep at most 1,000 detailed errors; always count all of them. The `value` can contain personal data; it goes only into the summary file (which expires with `processed/`), never into logs, metrics or events.

8. **Summary.** Write `summary.json` (path per step 2): `importId`, `sourceFileKey`, `sourceVersionId`, `datasetType`, `schoolId`, `status`, `reasonCode` (failures only), `totalRows`, `validRows`, `invalidRows`, `errorsTruncated` (bool), `errors[]`, `startedAt`, `finishedAt`, `durationMs`. If `validRows` is 0, `status` is `FAILED` with `reasonCode: NO_VALID_ROWS`, no `valid.jsonl` is written, and step 9 publishes `ImportFailed` instead of `ImportCompleted`.

9. **Publish.** Publish **last**, only after DynamoDB writes, `valid.jsonl` and `summary.json` all succeeded, so a consumer never receives an event whose claim-check file doesn't exist yet. `ImportCompleted` (`status` = `COMPLETED` if no invalid rows, else `COMPLETED_WITH_ERRORS`), exactly per `EVENT_CATALOGUE.md`, including the metadata block (`eventId` as a ULID, `correlationId` = `import_id`). Check the `put_events` response's `FailedEntryCount`; a non-zero count raises.

10. **Observability.**
    - Logger correlation ID = the S3 object key; also append `import_id`, `school_id`, `dataset_type` as keys.
    - Metrics (dimension `DatasetType`): `ProcessedRecordsCount`, `ValidRecordsCount`, `ValidationFailureCount`, `ImportsFailedCount` (also dimension `ReasonCode`), `PipelineLatency` (ms from S3 `eventTime` to publish).
    - Never log record contents or error `value`s: roster rows contain minors' personal data. Log counts, keys and error codes only.

**Document branch.** When `dataset_type` is `documents`, steps 4 to 9 are replaced by:

1. Pre-checks for size and metadata were done in step 2.
2. `get_object` with the `VersionId`, streaming the body into a `tempfile.SpooledTemporaryFile` (at most 25 MB) while computing its SHA-256. Run the content check from section 3 on the spooled file (`zipfile` needs a seekable file for the DOCX check). Mismatch → `ImportFailed` with `UNSUPPORTED_FORMAT`.
3. `document_id = "doc-" + sha256(f"{school_id}/{relative_path.lower()}")[:12]`, where `relative_path` is the key after `raw/documents/<school_id>/`. `version = "v-" + content_sha256[:12]`.
4. Server-side `copy_object` with `CopySource={"Bucket": …, "Key": …, "VersionId": version_id}` to `processed/documents/<school_id>/<document_id>/<version>/source.<ext>`. Copying the exact version you hashed means a concurrent overwrite of the key can't make the hash and the staged copy disagree. (Reading the object once to hash and check it is unavoidable; the copy avoids re-uploading it.)
5. Write `summary.json` beside it with `status: ACCEPTED`, then publish `DocumentAccepted` exactly per `EVENT_CATALOGUE.md`.
6. Metrics: `DocumentsAcceptedCount` (dimension `DatasetType=documents`).

Unexpected exceptions (S3 read failures other than 404, DynamoDB throttling beyond retries, malformed envelope, missing `versionId`) propagate so `BatchProcessor` reports the item as failed and SQS retries it. After three failures it lands in the DLQ, the DLQ alarm notifies ops, and the `dlq_replay` inspect action is how an admin sees what failed. Automatically publishing `ImportFailed` with `MOVED_TO_DLQ` (via a DLQ consumer) is phase 2 work.

## 6. Project structure

```
ingestion-service/
├── app.py
├── cdk.json
├── pyproject.toml
├── infra/
│   ├── shared_stack.py
│   ├── ingestion_stack.py
│   └── constructs/
│       ├── ingestion_bucket.py
│       ├── processing_queue.py   # includes the custom notification destination
│       ├── staging_tables.py
│       ├── worker_function.py
│       ├── dependencies_layer.py
│       └── ops_alarms.py
├── src/ingestion/
│   ├── handlers/
│   │   ├── etl_worker.py
│   │   └── dlq_replay.py
│   ├── datasets.py       # dataset registry: path name ↔ event name, extensions, size limits, schema, natural key
│   ├── schemas/          # pydantic models per dataset
│   ├── parsing/          # csv / json / jsonl streaming readers
│   ├── documents.py      # content checks, document_id / version
│   ├── storage/          # batch writer with UnprocessedItems retry, multipart JSONL writer
│   ├── events.py         # envelope builder + publisher
│   └── reporting.py      # summary + error collection
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── infra/
│   └── fixtures/         # sample valid, invalid, malformed and oversized files
├── scripts/
│   ├── build_layer.sh    # arm64 dependency layer build (section 2)
│   └── upload_sample.sh
├── .github/workflows/ci.yml
└── README.md
```

## 7. Testing and CI

**Unit tests (`moto`, no AWS account needed):**

- Valid CSV, JSON and JSONL for each dataset produce the right DynamoDB items, `valid.jsonl` lines, summary, and one `ImportCompleted` event.
- A file with mixed valid and invalid rows produces `COMPLETED_WITH_ERRORS` with correct line numbers and error codes; error `value`s never appear in captured log output.
- A JSON record with an extra field is a row error (`extra_forbidden`), not a file failure.
- Header mismatch, unsupported dataset type, wrong extension for the dataset, oversized file (each dataset's own limit), and missing `uploaded-by` each produce `ImportFailed` with the right `reasonCode` and a **successful** batch item result.
- An invalid `school_id` writes its summary under `processed/_rejected/` and publishes `metadata.schoolId: "UNKNOWN"`.
- A header-only file and a file where every row is invalid each produce `ImportFailed` with `NO_VALID_ROWS` and no `valid.jsonl`.
- Malformed JSON halfway through a file produces `ImportFailed` with `UNREADABLE`, and `list_multipart_uploads` shows no upload left open.
- The multipart writer: output under 5 MiB uses `put_object`; output over 5 MiB uses multipart with all non-final parts ≥ 5 MiB; an exception aborts.
- `s3:TestEvent` is ignored.
- Keys with spaces and special characters decode correctly.
- Redelivering the same message (same `versionId`) does not double-process. Re-uploading identical content (new `versionId`) **is** processed, under a new `import_id`.
- `UnprocessedItems` are retried (simulate with a stubbed client), and persistent failures mark the batch item failed.
- The event is published only after `valid.jsonl` and `summary.json` exist (assert ordering with a stubbed publisher).
- A generated fixture just under the 20 MB tabular limit processes with peak memory well under the configured size (assert with `tracemalloc`). Mark it `@pytest.mark.slow` if it is slow under moto, but run it in CI.
- Documents: a valid PDF produces a staged copy and one `DocumentAccepted`; re-uploading the same path with new content keeps `documentId` and changes `version`; missing audience → `MISSING_METADATA`; a `.pdf` that is really a text file → `UNSUPPORTED_FORMAT`; a `.docx` that is a ZIP without `word/document.xml` → `UNSUPPORTED_FORMAT`; a `.csv` under `raw/documents/` is rejected.

**Infrastructure tests (CDK `assertions`):**

- Visibility timeout equals 6 × worker timeout; `maxReceiveCount` is 3; DLQ retention is 14 days.
- Seven S3 notifications exist with the right prefix/suffix filters.
- The queue policy has exactly one statement for `s3.amazonaws.com`, and it includes both `aws:SourceArn` and `aws:SourceAccount` conditions.
- Lifecycle: `raw/` expires at 30 days with no transitions; `processed/` expires at 45 days; noncurrent versions at 30; multipart abort at 1.
- Bucket versioning is enabled; topic and queues enforce SSL.
- Event source mapping has `MaximumConcurrency: 5` and `ReportBatchItemFailures`.
- Worker role includes `s3:GetObjectVersion` on `raw/*` and `s3:AbortMultipartUpload` on `processed/*`; no other service principal or role is granted access to `IngestedRecords`.
- No IAM statement contains an `Action` of `*`; no AWS managed policies are attached.
- `cdk-nag` AwsSolutions checks pass; every suppression is one of those listed in section 4 and has a written reason.

**Integration test (opt-in, real account, `pytest -m integration`):** deploy to a `dev` stage, upload a fixture with the CLI, poll for `summary.json`, assert counts, and capture the `ImportCompleted` event via a temporary rule → SQS queue. This is also the check that the arm64 dependency layer imports correctly.

**CI (`.github/workflows/ci.yml`):** on every push run `ruff`, `mypy`, unit and infra tests, build the dependency layer, and `cdk synth`. Deployment uses GitHub OIDC with a role scoped to the dev account; no long-lived keys. Document in the README how the OIDC provider and role are created (one-time, outside these stacks).

## 8. Out of scope for v1 (document in README as "future work")

- Tabular files above 20 MB: split by byte range with a Step Functions Distributed Map.
- Presigned upload URLs through the authenticated API (Cognito).
- A per-tenant upload quota.
- A DLQ consumer that publishes `ImportFailed` with `MOVED_TO_DLQ`.
- OCR for scanned PDFs (a knowledge-service concern).

## 9. Instructions to the coding agent

1. Work in phases and stop for review after each: (a) `SharedStack` + bucket + queues + custom notification destination + arm64 dependency layer + infra tests; (b) dataset registry, parsing and schemas with unit tests; (c) worker handler end to end (including the multipart writer and document branch) with moto tests; (d) replay Lambda, alarms, metrics; (e) README and CI.
2. Run the full test suite and `cdk synth` at the end of every phase. Do not move on with failing tests.
3. Do not hard-code account IDs, regions or bucket names.
4. If this spec is ambiguous or seems wrong, state the issue and your proposed resolution before implementing it. Do not silently deviate.
5. Type-annotate everything in `src/`; keep handlers thin and the logic in importable, testable modules.

## 10. Deliverable: README contents

- Architecture diagram (Mermaid) showing S3 → SQS → worker → DynamoDB / S3 processed / EventBridge, plus DLQ, replay and alarms.
- Deploy instructions (`scripts/build_layer.sh`, `cdk bootstrap`, `cdk deploy --all`).
- Sample upload commands, e.g.:
  ```bash
  aws s3 cp tests/fixtures/marks_valid.csv \
    s3://school-ingestion-<account>-<region>/raw/marks/SCH-001/term4.csv \
    --metadata uploaded-by=USR-teacher-0042
  ```
- How to read the summary and error report, including where rejected files are reported (`processed/_rejected/`).
- DLQ runbook: force a failure, inspect with `dlq_replay`, fix, redrive, confirm.
- Architecture decisions: why max concurrency instead of reserved concurrency; why file-level idempotency keyed on `versionId` rather than ETag; why claim-check events; why the staging table is internal; why retention is 30/45 days (data minimisation for minors' data under POPIA, and longer than the event archive so replays work); why no Glacier or Standard-IA; why a 20 MB cap; why a custom notification destination for the queue policy.
- Cost notes and teardown (`cdk destroy --all`; empty the versioned bucket, including noncurrent versions, first).

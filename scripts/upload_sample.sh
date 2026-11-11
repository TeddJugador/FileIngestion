#!/usr/bin/env bash
# Upload a file to the ingestion bucket and print its summary.json when it's ready.
#
# Usage:
#   scripts/upload_sample.sh <file> <dataset_type> <school_id> [key_name]
#
# Examples:
#   scripts/upload_sample.sh tests/fixtures/marks_valid.csv marks SCH-001 term4.csv
#   AUDIENCE=STAFF,PARENT TITLE="Code of Conduct" \
#     scripts/upload_sample.sh policy.pdf documents SCH-001 policies/code_of_conduct.pdf
#
# Environment:
#   UPLOADED_BY  uploader's user ID (default USR-cli-0001)
#   AUDIENCE     documents only: STAFF, PARENT or STAFF,PARENT
#   TITLE        documents only, optional
#   WAIT_SECONDS how long to wait for the summary (default 90)
set -euo pipefail

if [[ $# -lt 3 ]]; then
  sed -n '2,17p' "$0"; exit 2
fi

FILE="$1"; DATASET="$2"; SCHOOL="$3"; NAME="${4:-$(basename "$FILE")}"
UPLOADED_BY="${UPLOADED_BY:-USR-cli-0001}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region)}}"
BUCKET="school-ingestion-${ACCOUNT}-${REGION}"
KEY="raw/${DATASET}/${SCHOOL}/${NAME}"

METADATA="uploaded-by=${UPLOADED_BY}"
if [[ "$DATASET" == "documents" ]]; then
  METADATA+=",audience=${AUDIENCE:?set AUDIENCE for documents, e.g. AUDIENCE=STAFF,PARENT}"
  [[ -n "${TITLE:-}" ]] && METADATA+=",title=${TITLE}"
fi

echo "Uploading ${FILE} -> s3://${BUCKET}/${KEY}"
VERSION_ID="$(aws s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$FILE" \
  --metadata "$METADATA" --query VersionId --output text)"

sha() { printf '%s' "$1" | sha256sum | cut -c1-12; }
IMPORT_ID="imp-$(sha "${BUCKET}/${KEY}/${VERSION_ID}")"
echo "versionId: ${VERSION_ID}"
echo "importId:  ${IMPORT_ID}"

# Where the summary can appear, depending on the outcome.
CANDIDATES=(
  "processed/${DATASET}/${SCHOOL}/${IMPORT_ID}/summary.json"
  "processed/_rejected/${IMPORT_ID}/summary.json"
)
if [[ "$DATASET" == "documents" ]]; then
  REL="${NAME}"
  DOC_ID="doc-$(sha "${SCHOOL}/${REL,,}")"
  VERSION="v-$(sha256sum "$FILE" | cut -c1-12)"
  CANDIDATES+=("processed/documents/${SCHOOL}/${DOC_ID}/${VERSION}/summary.json")
fi

echo -n "Waiting for the result"
for ((i = 0; i < WAIT_SECONDS; i += 3)); do
  for candidate in "${CANDIDATES[@]}"; do
    if aws s3api head-object --bucket "$BUCKET" --key "$candidate" >/dev/null 2>&1; then
      echo; echo "Summary: s3://${BUCKET}/${candidate}"
      aws s3 cp "s3://${BUCKET}/${candidate}" -
      exit 0
    fi
  done
  echo -n "."; sleep 3
done
echo; echo "No summary after ${WAIT_SECONDS}s. Check the worker's logs in CloudWatch and the DLQ." >&2
exit 1

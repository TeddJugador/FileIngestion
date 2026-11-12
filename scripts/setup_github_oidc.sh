#!/usr/bin/env bash
# One-time setup (outside the CDK stacks) so GitHub Actions can deploy with OIDC.
#
# Usage: scripts/setup_github_oidc.sh <github-owner> <repo-name>
#
# Creates (or updates):
#   - the GitHub OIDC identity provider in this AWS account (if missing)
#   - IAM role school-ingestion-github-deploy, trusted only by jobs in the "dev"
#     environment of <owner>/<repo>, allowed to:
#       * assume the CDK bootstrap roles (cdk deploy does the rest through them)
#       * run the integration tests (SCH-999 uploads, a temporary rule and queue)
# Prints the role ARN to save as the GitHub variable AWS_DEPLOY_ROLE_ARN.
#
# Repositories created (or renamed/transferred) after 2026-07-15 send an OIDC "sub" with
# immutable numeric IDs: repo:OWNER@OWNER-ID/REPO@REPO-ID:environment:dev. The trust
# policy accepts that form and the older name-only one. The IDs come from GitHub's public
# API; for a private repository, set GITHUB_OWNER_ID and GITHUB_REPO_ID yourself
# (shown in the repository's Settings > Actions > OIDC page).
set -euo pipefail

if [[ $# -ne 2 ]]; then sed -n '2,13p' "$0"; exit 2; fi
OWNER="$1"; REPO="$2"
ROLE_NAME="school-ingestion-github-deploy"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region)}}"
PARTITION="aws"
PROVIDER_HOST="token.actions.githubusercontent.com"
PROVIDER_ARN="arn:${PARTITION}:iam::${ACCOUNT}:oidc-provider/${PROVIDER_HOST}"
BUCKET="school-ingestion-${ACCOUNT}-${REGION}"

github_ids() {
  python3 - "$OWNER" "$REPO" <<'PY'
import json, sys, urllib.request
owner, repo = sys.argv[1], sys.argv[2]
request = urllib.request.Request(
    f"https://api.github.com/repos/{owner}/{repo}",
    headers={"User-Agent": "school-oidc-setup", "Accept": "application/vnd.github+json"},
)
with urllib.request.urlopen(request, timeout=20) as r:
    data = json.load(r)
print(data["owner"]["id"], data["id"])
PY
}
if [[ -z "${GITHUB_OWNER_ID:-}" || -z "${GITHUB_REPO_ID:-}" ]]; then
  if ! read -r GITHUB_OWNER_ID GITHUB_REPO_ID < <(github_ids); then
    echo "Couldn't read ${OWNER}/${REPO} from api.github.com (private?)." >&2
    echo "Set GITHUB_OWNER_ID and GITHUB_REPO_ID (Settings > Actions > OIDC) and rerun." >&2
    exit 1
  fi
fi
SUB_IMMUTABLE="repo:${OWNER}@${GITHUB_OWNER_ID}/${REPO}@${GITHUB_REPO_ID}:environment:dev"
SUB_LEGACY="repo:${OWNER}/${REPO}:environment:dev"
echo "Trusting OIDC subjects: ${SUB_IMMUTABLE} and ${SUB_LEGACY}"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "OIDC provider already exists"
else
  echo "Creating OIDC provider for ${PROVIDER_HOST}"
  aws iam create-open-id-connect-provider \
    --url "https://${PROVIDER_HOST}" --client-id-list sts.amazonaws.com >/dev/null
fi

TRUST=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${PROVIDER_ARN}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "${PROVIDER_HOST}:aud": "sts.amazonaws.com",
        "${PROVIDER_HOST}:sub": ["${SUB_IMMUTABLE}", "${SUB_LEGACY}"]
      }
    }
  }]
}
JSON
)

PERMISSIONS=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UseCdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:${PARTITION}:iam::${ACCOUNT}:role/cdk-hnb659fds-*"
    },
    {
      "Sid": "IntegrationReadStackOutputs",
      "Effect": "Allow",
      "Action": "cloudformation:DescribeStacks",
      "Resource": "arn:${PARTITION}:cloudformation:${REGION}:${ACCOUNT}:stack/SchoolIngestionStack/*"
    },
    {
      "Sid": "IntegrationUploadTestFiles",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:${PARTITION}:s3:::${BUCKET}/raw/*/SCH-999/*"
    },
    {
      "Sid": "IntegrationReadTestResults",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:${PARTITION}:s3:::${BUCKET}/processed/*/SCH-999/*"
    },
    {
      "Sid": "IntegrationTemporaryRule",
      "Effect": "Allow",
      "Action": ["events:PutRule", "events:PutTargets", "events:RemoveTargets", "events:DeleteRule", "events:DescribeRule"],
      "Resource": "arn:${PARTITION}:events:${REGION}:${ACCOUNT}:rule/SchoolEventBus/ingestion-it-*"
    },
    {
      "Sid": "IntegrationCanaryEvent",
      "Effect": "Allow",
      "Action": "events:PutEvents",
      "Resource": "arn:${PARTITION}:events:${REGION}:${ACCOUNT}:event-bus/SchoolEventBus",
      "Condition": {"StringEquals": {"events:source": "integration.test.canary"}}
    },
    {
      "Sid": "IntegrationTemporaryQueue",
      "Effect": "Allow",
      "Action": ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:ReceiveMessage"],
      "Resource": "arn:${PARTITION}:sqs:${REGION}:${ACCOUNT}:ingestion-it-*"
    }
  ]
}
JSON
)

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Updating role ${ROLE_NAME}"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST"
else
  echo "Creating role ${ROLE_NAME}"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" \
    --description "GitHub Actions (${OWNER}/${REPO}, environment dev): deploy and integration tests" \
    --max-session-duration 3600 >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name deploy-and-integration-test \
  --policy-document "$PERMISSIONS"

ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
cat <<TXT

Done. In GitHub: Settings -> Environments -> New environment "dev", then add these
environment variables (Variables tab, not Secrets: none of them is a secret):

  AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}
  AWS_REGION          = ${REGION}
  ALERT_EMAIL         = <your email>   (optional; keeps the alert subscription)

The next push to main will deploy and run the integration tests.
TXT

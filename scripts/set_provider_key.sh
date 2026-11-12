#!/usr/bin/env bash
# Store a model provider's API key in Secrets Manager, where the knowledge Lambdas read it.
#
# Usage: scripts/set_provider_key.sh <gemini|voyage|anthropic>
#
# The key is typed at a hidden prompt and passed to the AWS CLI on stdin, so it never
# appears in your shell history, the process list or a file.
set -euo pipefail

case "${1:-}" in
  gemini|voyage|anthropic) NAME="knowledge/$1" ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac

read -r -s -p "API key for ${NAME}: " KEY; echo
if [[ -z "$KEY" ]]; then echo "No key entered." >&2; exit 1; fi

if aws secretsmanager describe-secret --secret-id "$NAME" >/dev/null 2>&1; then
  printf '%s' "$KEY" | aws secretsmanager put-secret-value --secret-id "$NAME" \
    --secret-string file:///dev/stdin >/dev/null
  echo "Updated ${NAME}."
else
  printf '%s' "$KEY" | aws secretsmanager create-secret --name "$NAME" \
    --description "Knowledge service: $1 API key" --secret-string file:///dev/stdin >/dev/null
  echo "Created ${NAME}."
fi
unset KEY

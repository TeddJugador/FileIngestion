#!/usr/bin/env bash
# Knowledge service admin operations (invokes the admin_ops Lambda).
#
#   scripts/knowledge_ops.sh reindex [SCH-001] [--dry-run]   # all schools if omitted
#   scripts/knowledge_ops.sh dlq inspect [max]
#   scripts/knowledge_ops.sh dlq redrive [max-per-second]
#   scripts/knowledge_ops.sh dlq status [task-handle]
set -euo pipefail

STACK="SchoolKnowledgeStack"

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

function_name() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='AdminOpsFunctionName'].OutputValue" --output text
}

invoke() {
  local payload="$1" out
  out="$(mktemp)"
  trap 'rm -f "$out"' RETURN
  aws lambda invoke --function-name "$(function_name)" --cli-binary-format raw-in-base64-out \
    --cli-read-timeout 900 --payload "$payload" "$out" >/dev/null
  python3 -m json.tool "$out"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  reindex)
    shift
    school="" dry_run="false"
    for arg in "$@"; do
      case "$arg" in
        --dry-run) dry_run="true" ;;
        SCH-[0-9][0-9][0-9]) school="$arg" ;;
        *) echo "unknown argument: $arg" >&2; usage ;;
      esac
    done
    if [[ -n "$school" ]]; then
      invoke "{\"action\": \"reindex\", \"schoolId\": \"$school\", \"dryRun\": $dry_run}"
    else
      invoke "{\"action\": \"reindex\", \"dryRun\": $dry_run}"
    fi
    ;;
  dlq)
    [[ $# -ge 2 ]] || usage
    case "$2" in
      inspect) invoke "{\"action\": \"inspect_dlq\", \"max\": ${3:-10}}" ;;
      redrive) invoke "{\"action\": \"redrive_dlq\", \"maxPerSecond\": ${3:-10}}" ;;
      status)
        if [[ $# -ge 3 ]]; then
          invoke "{\"action\": \"dlq_status\", \"taskHandle\": \"$3\"}"
        else
          invoke '{"action": "dlq_status"}'
        fi
        ;;
      *) usage ;;
    esac
    ;;
  *) usage ;;
esac

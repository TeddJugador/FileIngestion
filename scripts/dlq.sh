#!/usr/bin/env bash
# Run the dlq_replay Lambda.
#
#   scripts/dlq.sh inspect [max]          # peek at up to max (default 10) messages
#   scripts/dlq.sh redrive [perSecond]    # move everything back to the main queue
#   scripts/dlq.sh status [taskHandle]    # progress of the latest (or a running) redrive
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  inspect) PAYLOAD="{\"action\":\"inspect\",\"max\":${2:-10}}" ;;
  redrive) PAYLOAD="{\"action\":\"redrive\",\"maxPerSecond\":${2:-10}}" ;;
  status)
    if [[ -n "${2:-}" ]]; then
      PAYLOAD="{\"action\":\"status\",\"taskHandle\":\"$2\"}"
    else
      PAYLOAD='{"action":"status"}'
    fi ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac

FUNCTION="$(aws cloudformation describe-stacks --stack-name SchoolIngestionStack \
  --query "Stacks[0].Outputs[?OutputKey=='DlqReplayFunctionName'].OutputValue" --output text)"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

aws lambda invoke --function-name "$FUNCTION" --cli-binary-format raw-in-base64-out \
  --payload "$PAYLOAD" "$OUT" --query FunctionError --output text >/dev/null
python3 -m json.tool "$OUT"

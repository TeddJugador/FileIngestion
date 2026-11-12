#!/usr/bin/env bash
# Build the arm64 dependency layers without Docker (works on x86 laptops and CI runners).
#
# Usage: scripts/build_layer.sh [all|ingestion|knowledge]   (default: all)
#   ingestion -> build/layer/python            (layer/requirements.txt)
#   knowledge -> build/knowledge-layer/python  (layer/knowledge-requirements.txt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_VERSION="$(tr -d '[:space:]' < "$ROOT/.lambda-python-version")"
ABI="cpython-${PY_VERSION/./}-aarch64-linux-gnu.so"

build() {
  local name="$1" requirements="$2" out="$3"; shift 3
  local target="$out/python"
  rm -rf "$out"
  mkdir -p "$target"

  python3 -m pip install \
    --platform manylinux2014_aarch64 \
    --implementation cp \
    --python-version "$PY_VERSION" \
    --only-binary=:all: \
    --target "$target" \
    --no-compile \
    --quiet \
    -r "$requirements"

  # The Lambda runtime ships boto3 and its dependency tree. A second copy in the layer
  # would shadow the runtime's versions (layers come first on sys.path).
  (
    cd "$target"
    rm -rf boto3 boto3-* botocore botocore-* s3transfer s3transfer-* \
           urllib3 urllib3-* dateutil python_dateutil-* six.py six-* jmespath jmespath-*
    find . -type d -name "__pycache__" -prune -exec rm -rf {} +
  )

  # Fail loudly if a compiled package came out for the wrong platform or ABI.
  local pkg so
  for pkg in "$@"; do
    so="$(find "$target/$pkg" -name '*.so' | head -n 1)"
    if [[ -z "$so" ]]; then
      echo "ERROR: no compiled extension found for $pkg ($name layer)" >&2; exit 1
    fi
    if [[ "$so" != *"$ABI" ]]; then
      echo "ERROR: $pkg extension is not cp${PY_VERSION/./}/aarch64: $so" >&2; exit 1
    fi
  done
  echo "${name} layer built for python${PY_VERSION}/arm64 at ${out} ($(du -sh "$out" | cut -f1))"
}

WHAT="${1:-all}"
case "$WHAT" in
  all|ingestion|knowledge) ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
if [[ "$WHAT" == all || "$WHAT" == ingestion ]]; then
  build ingestion "$ROOT/layer/requirements.txt" "$ROOT/build/layer" pydantic_core ijson
fi
if [[ "$WHAT" == all || "$WHAT" == knowledge ]]; then
  build knowledge "$ROOT/layer/knowledge-requirements.txt" "$ROOT/build/knowledge-layer" pydantic_core lxml
fi

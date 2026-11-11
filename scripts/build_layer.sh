#!/usr/bin/env bash
# Build the arm64 dependencies layer without Docker (works on x86 laptops and CI runners).
# Output: build/layer/python/...  (consumed by infra/constructs/dependencies_layer.py)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_VERSION="$(tr -d '[:space:]' < "$ROOT/.lambda-python-version")"
OUT="$ROOT/build/layer"
TARGET="$OUT/python"

rm -rf "$OUT"
mkdir -p "$TARGET"

python3 -m pip install \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --target "$TARGET" \
  --no-compile \
  --quiet \
  -r "$ROOT/layer/requirements.txt"

# The Lambda runtime ships boto3 and its dependency tree. Shipping a second copy in the
# layer would shadow the runtime's versions (layers come first on sys.path) and risk
# botocore/urllib3 mismatches, so remove them.
(
  cd "$TARGET"
  rm -rf boto3 boto3-* botocore botocore-* s3transfer s3transfer-* \
         urllib3 urllib3-* dateutil python_dateutil-* six.py six-* jmespath jmespath-*
  find . -type d -name "__pycache__" -prune -exec rm -rf {} +
  find . -type d -name "tests" -path "*/ijson/*" -prune -exec rm -rf {} +
)

# Fail loudly if a compiled package came out for the wrong platform/ABI.
for pkg in pydantic_core ijson; do
  so="$(find "$TARGET/$pkg" -name '*.so' | head -n 1)"
  if [[ -z "$so" ]]; then
    echo "ERROR: no compiled extension found for $pkg" >&2; exit 1
  fi
  case "$so" in
    *"cpython-${PY_VERSION/./}-aarch64-linux-gnu.so") ;;
    *) echo "ERROR: $pkg extension is not cp${PY_VERSION/./}/aarch64: $so" >&2; exit 1 ;;
  esac
done

echo "Layer built for python${PY_VERSION}/arm64 at $OUT ($(du -sh "$OUT" | cut -f1))"

#!/usr/bin/env bash
#
# build_lambda_package.sh — stage the Groww token-minter Lambda deploy bundle.
#
# DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md). The
# deployed Lambda must actually IMPORT its third-party runtime deps: boto3 is on
# the AWS Lambda Python runtime already, but growwapi + pyotp are NOT, so this
# script pip-installs them INTO the package staging dir alongside the handler
# source. Terraform's `archive_file` then zips the staging dir into the deploy
# zip; the `null_resource.build_lambda_package` re-runs this whenever the handler
# source or the pinned dep versions change.
#
# Run directly (`bash build_lambda_package.sh`) or via `deploy.sh` / Terraform.
# Env overrides (all optional, sane defaults):
#   BUILD_DIR         staging dir           (default ./.build/package)
#   GROWWAPI_VERSION  growwapi pip spec      (default growwapi)
#   PYOTP_VERSION     pyotp pip spec         (default pyotp)
#   LAMBDA_RUNTIME    e.g. python3.12        (informational; used to pick python)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd -- "$SCRIPT_DIR"

BUILD_DIR="${BUILD_DIR:-$SCRIPT_DIR/.build/package}"
GROWWAPI_VERSION="${GROWWAPI_VERSION:-growwapi}"
PYOTP_VERSION="${PYOTP_VERSION:-pyotp}"
LAMBDA_RUNTIME="${LAMBDA_RUNTIME:-python3.12}"

# Prefer the runtime-matching interpreter (e.g. python3.12), else fall back.
PYTHON_BIN="${LAMBDA_RUNTIME}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "build_lambda_package.sh: no python interpreter found (tried ${LAMBDA_RUNTIME} and python3)" >&2
  exit 1
fi

echo "build_lambda_package.sh: staging Lambda bundle into ${BUILD_DIR}"
echo "  interpreter : ${PYTHON_BIN}"
echo "  growwapi    : ${GROWWAPI_VERSION}"
echo "  pyotp       : ${PYOTP_VERSION}"

# Fresh staging dir every build so a removed dep never lingers in the zip.
rm -rf -- "$BUILD_DIR"
mkdir -p -- "$BUILD_DIR"

# 1) Handler source (self-contained; no brutex package import).
cp -- "$SCRIPT_DIR/handler.py" "$BUILD_DIR/handler.py"
cp -- "$SCRIPT_DIR/__init__.py" "$BUILD_DIR/__init__.py"

# 2) Third-party runtime deps INTO the package root (Lambda adds the package
#    root to sys.path, so a flat install is importable as `import pyotp` etc.).
#    boto3 is deliberately NOT installed — it ships with the AWS runtime.
#
#    CROSS-COMPILE for the Lambda runtime (x86_64 Linux, cpython 3.12), NOT the
#    build host. The operator builds on macOS/ARM64 (project rules §8: Apple M4
#    Pro), so a plain `pip install --target` would vendor macOS/ARM wheels; the
#    Lambda runtime is x86_64 Linux and the handler's `from growwapi import
#    GrowwAPI` drags in native-extension deps (pynacl, pydantic-core, pandas,
#    aiohttp, protobuf) whose macOS/ARM wheels would ImportError on the first
#    real mint. The `--platform manylinux2014_x86_64 --only-binary=:all:
#    --python-version 3.12 --implementation cp --abi cp312` flags force pip to
#    resolve manylinux x86_64 cp312 wheels for the WHOLE transitive tree, so the
#    bundle is Linux-x86_64 regardless of the build host. `--only-binary=:all:`
#    forbids any from-source build (which would emit host-native binaries), so a
#    dep with no matching wheel FAILS the build loudly rather than silently
#    shipping a broken zip.
"$PYTHON_BIN" -m pip install \
  --quiet \
  --no-compile \
  --target "$BUILD_DIR" \
  --platform manylinux2014_x86_64 --only-binary=:all: \
  --python-version 3.12 --implementation cp --abi cp312 \
  "$GROWWAPI_VERSION" \
  "$PYOTP_VERSION"

echo "build_lambda_package.sh: bundle ready at ${BUILD_DIR} (cross-compiled for Lambda x86_64 Linux / cpython 3.12)"

#!/usr/bin/env bash
#
# build_lambda_package.sh — stage the Groww token-minter Lambda deploy bundle.
#
# DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md) +
# DEC-MINTER-PURE-PYTHON-001 (docs/MIGRATION.md #1069). The deployed Lambda must
# actually IMPORT its third-party runtime deps at runtime: boto3 is on the AWS
# Lambda Python runtime already, but pyotp is NOT, so this script pip-installs it
# INTO the package staging dir alongside the handler source. Terraform's
# `archive_file` then zips the staging dir into the deploy zip; the
# `null_resource.build_lambda_package` re-runs it whenever the handler source or
# the pinned pyotp version changes.
#
# NO DOCKER NEEDED — the ONLY bundled dep is pyotp, a pure-Python UNIVERSAL wheel
# (`py3-none-any`, zero runtime deps), so `pip install --target` produces ZERO
# platform-specific artifacts and the bundle is byte-portable across macOS/ARM64
# (project rules §8: Apple M4 Pro) and the Lambda's x86_64 Linux / cpython 3.12
# runtime. The token mint is a pure-stdlib HTTPS POST in handler.py (a byte-for-
# byte reimplementation of growwapi 1.5.0 `GrowwAPI.get_access_token`), so the
# bundle carries NO growwapi / pandas / pynacl / pydantic-core — i.e. no native
# extensions, hence no cross-compile flags, no `--platform`/`--only-binary`
# gymnastics, and no Docker (which the earlier growwapi-based build needed because
# growwapi's transitive `clint` ships sdist-only). One deterministic build path.
#
# Run directly (`bash build_lambda_package.sh`) or via `deploy.sh` / Terraform.
# Env overrides (all optional, sane defaults):
#   BUILD_DIR       staging dir       (default ./.build/package)
#   PYOTP_VERSION   pyotp pip spec    (default pyotp==2.10.0 — PINNED)
#   LAMBDA_RUNTIME  e.g. python3.12   (informational; used to pick the python)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd -- "$SCRIPT_DIR"

BUILD_DIR="${BUILD_DIR:-$SCRIPT_DIR/.build/package}"
# PINNED by default so pip never backtracks hunting for a consistent set. 2.10.0
# is the latest within BruteX's pyproject.toml `pyotp>=2.9,<3.0`, so the Lambda
# and the app share one dependency truth. Override to pin differently or to float.
PYOTP_VERSION="${PYOTP_VERSION:-pyotp==2.10.0}"
LAMBDA_RUNTIME="${LAMBDA_RUNTIME:-python3.12}"

# Prefer the runtime-matching interpreter (e.g. python3.12), else fall back. Since
# pyotp is a universal wheel there is no cross-compile concern — any cpython 3.x
# resolves the SAME `py3-none-any` artifact — but matching the runtime is tidy.
PYTHON_BIN="${LAMBDA_RUNTIME}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "build_lambda_package.sh: no python interpreter found (tried ${LAMBDA_RUNTIME} and python3)" >&2
  exit 1
fi

echo "build_lambda_package.sh: staging Lambda bundle into ${BUILD_DIR}"
echo "  pyotp       : ${PYOTP_VERSION} (pure-Python universal wheel — no Docker needed)"
echo "  python      : ${PYTHON_BIN}"

# Fresh staging dir every build so a removed dep never lingers in the zip.
rm -rf -- "$BUILD_DIR"
mkdir -p -- "$BUILD_DIR"

# Handler source (self-contained; no brutex package import, no growwapi).
cp -- "$SCRIPT_DIR/handler.py" "$BUILD_DIR/handler.py"
cp -- "$SCRIPT_DIR/__init__.py" "$BUILD_DIR/__init__.py"

# Install pyotp INTO the staging dir. A plain `--target` install is fully
# platform-independent here because pyotp is `py3-none-any`, so the vendored
# artifacts are pure `.py` (no `.so`/`.dylib`/`.pyd`) and run unchanged on the
# Lambda's x86_64 Linux runtime regardless of the build host. `--no-compile`
# keeps the bundle to source only (no host-specific `.pyc`).
echo "  installing pyotp into the staging dir (no cross-compile flags, no Docker)"
"$PYTHON_BIN" -m pip install \
  --quiet \
  --no-compile \
  --target "$BUILD_DIR" \
  "$PYOTP_VERSION"

echo "build_lambda_package.sh: bundle ready at ${BUILD_DIR} (pure-Python, runs on Lambda x86_64 Linux / cpython 3.12)"

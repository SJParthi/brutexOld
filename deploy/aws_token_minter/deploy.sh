#!/usr/bin/env bash
#
# deploy.sh — one-command deploy of the centralized Groww token-minter stack.
#
# DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md,
# docs/runbooks/aws-token-minter.md) + DEC-MINTER-PURE-PYTHON-001
# (docs/MIGRATION.md #1069). Runs, in order:
#   1. build_lambda_package.sh  — bundle the handler + pyotp into the deploy zip
#      (the Lambda must actually import pyotp at runtime). The build is a SINGLE
#      deterministic pip install with NO DOCKER: pyotp is a pure-Python universal
#      wheel (`py3-none-any`, zero deps) so the bundle has zero platform-specific
#      artifacts and builds identically on macOS/ARM64 and Linux. The token mint
#      is a pure-stdlib HTTPS POST in handler.py, so no growwapi (and its heavy
#      native-extension tree) is bundled — that is what removed the Docker need.
#   2. terraform init           — fetch the aws + archive + null providers.
#   3. terraform apply          — SHOWS the plan and requires the operator to
#      type `yes` at Terraform's built-in confirmation prompt. This script NEVER
#      passes -auto-approve; a human confirms every apply.
#
# Region (ap-south-1 / Mumbai) and the /tickvault/prod/groww/* SSM path prefix
# are the tfvars DEFAULTS (variables.tf), so no -var is needed for them. Supply
# the alert email inline OR export ALERT_EMAIL; other vars can be overridden with
# extra `-var 'name=value'` args passed straight through to `terraform apply`.
#
# Usage:
#   cd deploy/aws_token_minter && ./deploy.sh                      # uses $ALERT_EMAIL if set
#   ALERT_EMAIL=you@example.com ./deploy.sh
#   ./deploy.sh -var 'alert_email=you@example.com'
#   ./deploy.sh -var 'schedule_expression=cron(35 0 * * ? *)'     # override any default

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd -- "$SCRIPT_DIR"

# Region + SSM path prefix are the tfvars defaults; echo them so the operator
# sees exactly what will be deployed before confirming.
REGION_DEFAULT="ap-south-1"
PARAM_PREFIX_DEFAULT="/tickvault/prod/groww"

echo "== Groww token-minter deploy =="
echo "  region (default)       : ${REGION_DEFAULT}"
echo "  SSM path prefix (default): ${PARAM_PREFIX_DEFAULT}/{api-key,totp-secret,access-token}"
echo

if ! command -v terraform >/dev/null 2>&1; then
  echo "deploy.sh: terraform not found on PATH — install Terraform >= 1.5 first." >&2
  exit 1
fi

# Fold an ALERT_EMAIL env var into a -var if the operator did not pass one.
EXTRA_ARGS=("$@")
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  ALREADY_SET=false
  for arg in "${EXTRA_ARGS[@]}"; do
    case "$arg" in
    *alert_email=*) ALREADY_SET=true ;;
    esac
  done
  if [[ "$ALREADY_SET" == false ]]; then
    EXTRA_ARGS+=("-var" "alert_email=${ALERT_EMAIL}")
  fi
fi

# 1) Bundle the handler + pyotp into the deploy zip. ONE deterministic pip
#    install, no Docker: pyotp is a pure-Python universal wheel so the bundle has
#    zero platform-specific artifacts and builds the same on any host.
echo "== 1/3  building Lambda deploy bundle (handler + pyotp — pure Python, no Docker) =="
bash build_lambda_package.sh

# 2) Initialise Terraform (idempotent; fetches providers).
echo "== 2/3  terraform init =="
terraform init -input=false

# 3) Apply — Terraform SHOWS the plan and prompts for `yes`. NO -auto-approve.
echo "== 3/3  terraform apply (review the plan, then type 'yes' to confirm) =="
terraform apply "${EXTRA_ARGS[@]}"

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

# Pre-flight: the custom KMS key is a Terraform DATA source (referenced, never
# created — the operator directive in ADR-0014 §5 keeps the key OUT of this stack's
# lifecycle so `terraform destroy` can NEVER delete the key guarding the real Groww
# creds). The stack encrypts the access-token param it CREATES under this key, so the
# key must exist BEFORE apply regardless of where the two creds live — else
# `terraform apply` dies deep in the plan with the opaque
#   Error: reading KMS Key (alias/tickvault-groww): couldn't find resource
# so we probe it here FIRST and print the exact one-time create commands instead.
# Best-effort: only runs when the aws CLI is present, respects an operator
# `-var 'kms_key_alias=…'` / `-var 'region=…'` override, and NEVER blocks on any
# other outcome (a describe-key error that is NOT "not found" — an auth/network
# hiccup — is left for Terraform to surface authoritatively).
#
# NOTE: the two Groww creds do NOT need to live under this key — by default the
# stack also grants the minter Decrypt on the AWS-managed alias/aws/ssm key scoped
# to SSM-only (var allow_ssm_default_key_decrypt=true), so creds under the default
# SSM key are read fine with no re-put.
KMS_KEY_ALIAS_DEFAULT="alias/tickvault-groww"
KMS_KEY_ALIAS="$KMS_KEY_ALIAS_DEFAULT"
REGION="$REGION_DEFAULT"
for arg in "$@"; do
  case "$arg" in
  kms_key_alias=*) KMS_KEY_ALIAS="${arg#kms_key_alias=}" ;;
  region=*) REGION="${arg#region=}" ;;
  esac
done

if command -v aws >/dev/null 2>&1; then
  if ! aws kms describe-key --region "$REGION" --key-id "$KMS_KEY_ALIAS" \
    >/dev/null 2>&1; then
    {
      echo "deploy.sh: the KMS key ${KMS_KEY_ALIAS} was not found in ${REGION}."
      echo
      echo "This stack REFERENCES that key (it never creates it — ADR-0014 §5) and"
      echo "encrypts the access-token it writes under it, so the key must exist BEFORE"
      echo "apply. Create it once:"
      echo
      echo "  aws kms create-key --region ${REGION} \\"
      echo "    --description 'Groww token-minter SecureString key (tickvault)' \\"
      echo "    --query KeyMetadata.KeyId --output text"
      echo "  # then, using the KeyId printed above:"
      echo "  aws kms create-alias --region ${REGION} \\"
      echo "    --alias-name ${KMS_KEY_ALIAS} --target-key-id <KeyId-from-above>"
      echo
      echo "You do NOT need to move your two Groww creds under this key — the minter"
      echo "is granted SSM-scoped Decrypt on the default alias/aws/ssm key too (var"
      echo "allow_ssm_default_key_decrypt, default true). The access-token param is"
      echo "created + encrypted under this key BY the stack — you do NOT pre-create it."
      echo
      echo "Then re-run ./deploy.sh. See docs/runbooks/aws-token-deploy-guide.md §1d."
    } >&2
    exit 1
  fi
fi

# Fold an ALERT_EMAIL env var into a -var if the operator did not pass one.
#
# EXTRA_ARGS is initialised as an empty array, then filled from "$@". Every
# expansion of it below uses the portable `${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}`
# guard: on macOS's default bash 3.2, `set -u` (nounset) treats an EMPTY array's
# `"${arr[@]}"` as an unbound variable and ABORTS the script — so a plain
# `"${EXTRA_ARGS[@]}"` dies with `EXTRA_ARGS[@]: unbound variable` right before
# `terraform apply` when no extra args are passed. The `+`-alternate form expands
# to NOTHING when the array is unset/empty and to the quoted elements otherwise,
# and works identically on bash 3.2 and bash 4+.
EXTRA_ARGS=()
if [[ "$#" -gt 0 ]]; then
  EXTRA_ARGS=("$@")
fi
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  ALREADY_SET=false
  for arg in "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"; do
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
terraform apply "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

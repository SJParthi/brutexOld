#!/usr/bin/env bash
#
# deploy.sh — one-command deploy of the centralized Groww token-minter stack.
#
# DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md,
# docs/runbooks/aws-token-minter.md) + DEC-MINTER-PURE-PYTHON-001
# (docs/MIGRATION.md #1069) + DEC-DEPLOY-REMOTE-STATE-001
# (docs/runbooks/aws-token-deploy-guide.md) + DEC-DEPLOY-AUTO-FIRST-MINT-001
# (auto-adopt preflight + zero-touch first mint). Runs, in order:
#   1. build_lambda_package.sh  — bundle the handler + pyotp into the deploy zip
#      (the Lambda must actually import pyotp at runtime). The build is a SINGLE
#      deterministic pip install with NO DOCKER: pyotp is a pure-Python universal
#      wheel (`py3-none-any`, zero deps) so the bundle has zero platform-specific
#      artifacts and builds identically on macOS/ARM64 and Linux. The token mint
#      is a pure-stdlib HTTPS POST in handler.py, so no growwapi (and its heavy
#      native-extension tree) is bundled — that is what removed the Docker need.
#   2. bootstrap remote state   — IDEMPOTENTLY create the S3 state bucket +
#      DynamoDB lock table via the AWS CLI (check-then-create), so terraform can
#      use them as a REMOTE backend (fresh-clone-repeatable state; no local
#      terraform.tfstate to lose). Re-runs are no-ops.
#   3. terraform init           — with -backend-config pointing at that bucket /
#      table / region, so state lives in AWS (not on disk).
#   4. adopt preflight          — AUTO-IMPORT each of the 7 name-fixed resources
#      that already EXISTS in AWS but is MISSING from the (possibly empty) remote
#      state, so a stack first created under the OLD LOCAL state (or out-of-band)
#      never 409s the apply with ParameterAlreadyExists / EntityAlreadyExists.
#      Idempotent: already-in-state and not-present-in-AWS are honest no-ops; a
#      FAILED `terraform import` HARD-STOPS the deploy (continuing into apply
#      would just re-fail with the same conflict).
#   5. terraform apply          — SHOWS the plan and requires the operator to type
#      `yes` at Terraform's built-in confirmation prompt. This script NEVER passes
#      -auto-approve; a human confirms every apply.
#   6. first token mint         — invokes the freshly-applied Lambda ONCE and
#      verifies its response reports ok:true, so the operator NEVER runs a manual
#      `aws lambda invoke`: the shared access-token param is live the moment the
#      deploy finishes instead of staying empty until the next 06:05 IST cron
#      (zero-touch, project rules §11 AUTOMATED). Re-running deploy.sh re-mints
#      once per deploy — harmless: the Lambda is the SOLE token writer and the
#      SSM write is idempotent last-writer-wins (PutParameter Overwrite=True,
#      docs/adr/0014-centralized-token-minter.md). A failed mint prints
#      "FIRST MINT: FAIL" with the exact error text and exits NON-ZERO, so a
#      broken credential / first mint is NEVER silent.
#
# WHY REMOTE STATE (DEC-DEPLOY-REMOTE-STATE-001): with LOCAL state a FRESH CLONE
# has no terraform.tfstate, so `apply` tries to RE-CREATE existing AWS resources
# and errors. Remote state (S3 + DynamoDB lock) makes the deploy idempotent from
# ANY clone on ANY machine, any number of times.
#
# ONE-TIME MIGRATION for an operator who ALREADY deployed with LOCAL state:
#   run ONCE, by hand, instead of step 3's plain init:
#       terraform init -migrate-state \
#         -backend-config="bucket=tickvault-tfstate-<ACCOUNT_ID>" \
#         -backend-config="dynamodb_table=tickvault-tfstate-lock" \
#         -backend-config="region=ap-south-1"
#   answer `yes` when Terraform offers to copy the existing local state to S3.
#   After that, every fresh clone reads the remote state and `./deploy.sh` is
#   idempotent. See docs/runbooks/aws-token-deploy-guide.md.
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

# Remote-state backend resource names (DEC-DEPLOY-REMOTE-STATE-001). The bucket
# is account-scoped (S3 bucket names are globally unique) so it is derived from
# the caller's account id below; the lock table is a fixed per-account name.
STATE_REGION="${REGION_DEFAULT}"
STATE_LOCK_TABLE="tickvault-tfstate-lock"

echo "== Groww token-minter deploy =="
echo "  region (default)       : ${REGION_DEFAULT}"
echo "  SSM path prefix (default): ${PARAM_PREFIX_DEFAULT}/{api-key,totp-secret,access-token}"
echo "  terraform state          : REMOTE (S3 + DynamoDB lock, ${STATE_REGION})"
echo

if ! command -v terraform >/dev/null 2>&1; then
  echo "deploy.sh: terraform not found on PATH — install Terraform >= 1.5 first." >&2
  exit 1
fi
if ! command -v aws >/dev/null 2>&1; then
  echo "deploy.sh: aws CLI not found on PATH — install the AWS CLI v2 first." >&2
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
# Name-fixed resource identifiers (defaults mirror variables.tf) — used by the
# adopt preflight (step 4) + the first mint (step 6). Operator `-var` overrides
# are honored so the preflight adopts / the mint invokes the RIGHT resources.
LAMBDA_FUNCTION_NAME="groww-token-minter"
PARAM_PREFIX="$PARAM_PREFIX_DEFAULT"
TOKEN_PARAM_NAME="access-token"
for arg in "$@"; do
  case "$arg" in
  kms_key_alias=*) KMS_KEY_ALIAS="${arg#kms_key_alias=}" ;;
  region=*) REGION="${arg#region=}" ;;
  lambda_function_name=*) LAMBDA_FUNCTION_NAME="${arg#lambda_function_name=}" ;;
  param_prefix=*) PARAM_PREFIX="${arg#param_prefix=}" ;;
  token_param_name=*) TOKEN_PARAM_NAME="${arg#token_param_name=}" ;;
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
echo "== 1/6  building Lambda deploy bundle (handler + pyotp — pure Python, no Docker) =="
bash build_lambda_package.sh

# 2) Bootstrap the remote-state backend (idempotent, check-then-create).
#    S3 bucket names are globally unique, so scope the bucket to the account id.
echo "== 2/6  bootstrapping remote state (S3 bucket + DynamoDB lock table) =="
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ -z "${ACCOUNT_ID}" || "${ACCOUNT_ID}" == "None" ]]; then
  echo "deploy.sh: could not resolve AWS account id (aws sts get-caller-identity)." >&2
  echo "           run 'aws configure' with a valid admin identity first." >&2
  exit 1
fi
STATE_BUCKET="tickvault-tfstate-${ACCOUNT_ID}"
echo "  state bucket : ${STATE_BUCKET}"
echo "  lock table   : ${STATE_LOCK_TABLE}"

# 2a) S3 state bucket — create only if absent; then harden (versioning + SSE +
#     public-access-block). head-bucket returns non-zero when the bucket does not
#     exist (or is not ours), so it is the idempotency guard.
if aws s3api head-bucket --bucket "${STATE_BUCKET}" >/dev/null 2>&1; then
  echo "  s3: bucket already exists — reusing (no-op)"
else
  echo "  s3: creating bucket ${STATE_BUCKET}"
  # us-east-1 rejects a LocationConstraint; every other region requires one.
  if [[ "${STATE_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "${STATE_BUCKET}" \
      --region "${STATE_REGION}" >/dev/null
  else
    aws s3api create-bucket \
      --bucket "${STATE_BUCKET}" \
      --region "${STATE_REGION}" \
      --create-bucket-configuration "LocationConstraint=${STATE_REGION}" >/dev/null
  fi
fi
# Harden unconditionally — these put-* calls are idempotent (safe to re-apply),
# so a bucket created by an earlier partial run is still fully hardened.
aws s3api put-bucket-versioning \
  --bucket "${STATE_BUCKET}" \
  --versioning-configuration "Status=Enabled" >/dev/null
aws s3api put-bucket-encryption \
  --bucket "${STATE_BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
aws s3api put-public-access-block \
  --bucket "${STATE_BUCKET}" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null
echo "  s3: versioning + AES256 SSE + public-access-block enforced"

# 2b) DynamoDB lock table — create only if absent (PAY_PER_REQUEST, key LockID).
if aws dynamodb describe-table \
  --table-name "${STATE_LOCK_TABLE}" \
  --region "${STATE_REGION}" >/dev/null 2>&1; then
  echo "  dynamodb: lock table already exists — reusing (no-op)"
else
  echo "  dynamodb: creating lock table ${STATE_LOCK_TABLE}"
  aws dynamodb create-table \
    --table-name "${STATE_LOCK_TABLE}" \
    --region "${STATE_REGION}" \
    --attribute-definitions "AttributeName=LockID,AttributeType=S" \
    --key-schema "AttributeName=LockID,KeyType=HASH" \
    --billing-mode "PAY_PER_REQUEST" >/dev/null
  echo "  dynamodb: waiting for the lock table to become ACTIVE"
  aws dynamodb wait table-exists \
    --table-name "${STATE_LOCK_TABLE}" \
    --region "${STATE_REGION}"
fi

# 3) Initialise Terraform with the REMOTE backend (idempotent; fetches providers).
#    -reconfigure is safe on a fresh clone (nothing to reconfigure) and on a
#    machine that already inited (re-points at the same backend). It deliberately
#    does NOT migrate state; an operator with existing LOCAL state runs the
#    one-time `terraform init -migrate-state ...` by hand (see the header +
#    docs/runbooks/aws-token-deploy-guide.md).
echo "== 3/6  terraform init (remote S3 + DynamoDB backend) =="
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="dynamodb_table=${STATE_LOCK_TABLE}" \
  -backend-config="region=${STATE_REGION}"

# 4) Adopt pre-existing AWS resources into the remote state (idempotent).
#    DEC-DEPLOY-AUTO-FIRST-MINT-001: a stack first deployed under the OLD LOCAL
#    state (or created out-of-band) already has its name-fixed resources LIVE in
#    AWS while the shared remote state is EMPTY — a plain apply then dies with
#    ParameterAlreadyExists / EntityAlreadyExists (409) on every one of them.
#    For each ADOPTABLE address: absent from state AND present in AWS =>
#    `terraform import` (adopted). Already-in-state and not-present-in-AWS are
#    honest no-ops, so a healthy re-run prints seven no-op lines and moves on.
#    ONLY these 7 resources carry operator-visible FIXED names that can collide;
#    everything else in the stack either derives from them at apply time or is
#    created with idempotent put semantics (event rule/target, inline role
#    policies, alarms, SNS topic).

resource_in_state() {
  terraform state show "$1" >/dev/null 2>&1
}

resource_exists_in_aws() {
  case "$1" in
  ssm-param)
    aws ssm get-parameter --region "$REGION" --name "$2" >/dev/null 2>&1
    ;;
  iam-role)
    aws iam get-role --role-name "$2" >/dev/null 2>&1
    ;;
  log-group)
    # describe-log-groups exits 0 even on no match, and --log-group-name-prefix
    # is a PREFIX filter — so require an EXACT logGroupName match.
    FOUND_LOG_GROUP="$(aws logs describe-log-groups --region "$REGION" \
      --log-group-name-prefix "$2" \
      --query "logGroups[?logGroupName=='$2'].logGroupName" \
      --output text 2>/dev/null || true)"
    [ "$FOUND_LOG_GROUP" = "$2" ]
    ;;
  lambda-function)
    aws lambda get-function --region "$REGION" --function-name "$2" >/dev/null 2>&1
    ;;
  lambda-permission)
    # Import id is <function_name>/<statement_id>; the permission exists iff the
    # function's resource policy carries that statement id. Pipeline-free (a
    # `| grep -q` under `set -o pipefail` can misread an early-exit SIGPIPE as
    # "not present"): capture, then substring-match with `case`.
    POLICY_TEXT="$(aws lambda get-policy --region "$REGION" --function-name "${2%%/*}" \
      --query Policy --output text 2>/dev/null || true)"
    case "$POLICY_TEXT" in
    *"${2##*/}"*) return 0 ;;
    *) return 1 ;;
    esac
    ;;
  *)
    return 1
    ;;
  esac
}

adopt_resource() {
  ADOPT_ADDR="$1"
  ADOPT_KIND="$2"
  ADOPT_ID="$3"
  if resource_in_state "$ADOPT_ADDR"; then
    echo "  adopt: ${ADOPT_ADDR} — already in state (no-op)"
    return 0
  fi
  if ! resource_exists_in_aws "$ADOPT_KIND" "$ADOPT_ID"; then
    echo "  adopt: ${ADOPT_ADDR} — not present in AWS (apply will create it)"
    return 0
  fi
  echo "  adopt: ${ADOPT_ADDR} — exists in AWS but missing from state; importing ${ADOPT_ID}"
  # The operator's -var overrides ride along so import evaluates the SAME config
  # (provider region, names) the apply will. stderr is NOT redirected — on a
  # failure the verbatim terraform error is what the operator must see.
  if ! terraform import -input=false "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" "$ADOPT_ADDR" "$ADOPT_ID"; then
    echo "deploy.sh: FATAL — terraform import failed for ${ADOPT_ADDR} (id: ${ADOPT_ID})." >&2
    echo "           The verbatim terraform error is printed above. Fix it, then re-run" >&2
    echo "           ./deploy.sh. Continuing into 'terraform apply' would just re-fail" >&2
    echo "           with the same already-exists conflict, so the deploy stops HERE." >&2
    exit 1
  fi
  echo "  adopt: ${ADOPT_ADDR} — adopted into the remote state"
}

echo "== 4/6  adopting pre-existing AWS resources into the remote state (idempotent) =="
adopt_resource "aws_ssm_parameter.access_token" ssm-param "${PARAM_PREFIX}/${TOKEN_PARAM_NAME}"
adopt_resource "aws_iam_role.minter" iam-role "${LAMBDA_FUNCTION_NAME}-minter"
adopt_resource "aws_iam_role.reader_tickvault" iam-role "${LAMBDA_FUNCTION_NAME}-reader-tickvault"
adopt_resource "aws_iam_role.reader_brutex" iam-role "${LAMBDA_FUNCTION_NAME}-reader-brutex"
adopt_resource "aws_cloudwatch_log_group.minter" log-group "/aws/lambda/${LAMBDA_FUNCTION_NAME}"
adopt_resource "aws_lambda_function.minter" lambda-function "${LAMBDA_FUNCTION_NAME}"
adopt_resource "aws_lambda_permission.allow_eventbridge" lambda-permission "${LAMBDA_FUNCTION_NAME}/AllowEventBridgeInvoke"

# 5) Apply — Terraform SHOWS the plan and prompts for `yes`. NO -auto-approve.
echo "== 5/6  terraform apply (review the plan, then type 'yes' to confirm) =="
terraform apply "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

# 6) First token mint — the deploy triggers the FIRST mint itself so the shared
#    access-token param is live NOW, not at the next 06:05 IST cron
#    (DEC-DEPLOY-AUTO-FIRST-MINT-001; zero-touch, project rules §11 AUTOMATED).
#    Reached ONLY after a successful apply (`set -e` + terraform's own `yes`
#    confirmation abort every earlier failure). Idempotent: a re-run re-mints
#    once per deploy — harmless, the Lambda is the SOLE token writer and the SSM
#    write is last-writer-wins (PutParameter Overwrite=True). The Lambda name is
#    read from the terraform output (authoritative post-apply), falling back to
#    the resolved variable if the output read fails.
echo "== 6/6  first token mint (auto-invoke — no manual 'aws lambda invoke' needed) =="
MINTER_LAMBDA_NAME="$(terraform output -raw minter_lambda_name 2>/dev/null || echo "$LAMBDA_FUNCTION_NAME")"
MINT_RESPONSE="$(mktemp "${TMPDIR:-/tmp}/groww-first-mint.XXXXXX")"
if ! INVOKE_META="$(aws lambda invoke \
  --region "$REGION" \
  --function-name "$MINTER_LAMBDA_NAME" \
  --cli-read-timeout 120 \
  "$MINT_RESPONSE" 2>&1)"; then
  echo "FIRST MINT: FAIL — 'aws lambda invoke' errored for ${MINTER_LAMBDA_NAME}:" >&2
  echo "$INVOKE_META" >&2
  rm -f "$MINT_RESPONSE"
  exit 1
fi
# The handler returns {"ok": true, "attempts": N, "token_param": "..."} on
# success; on a handler failure the payload is {"errorMessage": ..., ...} (and
# the invoke metadata carries FunctionError), which this check rejects.
if grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$MINT_RESPONSE"; then
  echo "FIRST MINT: PASS — $(cat "$MINT_RESPONSE")"
  echo "  the shared token param is live; the 06:05 IST daily cron takes over from here."
  rm -f "$MINT_RESPONSE"
else
  echo "FIRST MINT: FAIL — the minter Lambda did not report ok:true." >&2
  echo "  response payload : $(cat "$MINT_RESPONSE")" >&2
  echo "  invoke metadata  : ${INVOKE_META}" >&2
  echo "  Check CloudWatch Logs (/aws/lambda/${MINTER_LAMBDA_NAME}) for the sanitized" >&2
  echo "  reason, fix the credential/param, then re-run ./deploy.sh (or invoke the" >&2
  echo "  Lambda by hand — docs/runbooks/aws-token-minter.md §5)." >&2
  rm -f "$MINT_RESPONSE"
  exit 1
fi

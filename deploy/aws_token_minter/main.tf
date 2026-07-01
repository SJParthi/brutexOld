# Centralized Groww daily-token minter — AWS infrastructure
# (DEC-TOKEN-MINTER-CENTRAL-001, docs/adr/0014-centralized-token-minter.md).
#
# ONE Lambda is the SOLE writer of the shared Groww daily access token, stored
# in SSM Parameter Store (SecureString, KMS-encrypted). Both TickVault (live)
# and BruteX (backtest) read that token READ-ONLY, which permanently kills the
# multi-app "minting storm". Single-writer => NO DynamoDB / no CAS is needed.
#
# This stack PROVISIONS ONLY: the token param, the minter Lambda + its
# EventBridge schedule, the CloudWatch alarm + SNS topic, and three
# least-privilege IAM roles. It REFERENCES (never creates/manages) the EXISTING
# KMS key + the two credential params via `data` sources.

provider "aws" {
  region = var.region
}

# --------------------------------------------------------------------------- #
# Existing resources referenced (NOT created/managed by this stack).
# --------------------------------------------------------------------------- #

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# The EXISTING KMS key that encrypts the Groww SecureStrings, by alias.
data "aws_kms_key" "groww" {
  key_id = var.kms_key_alias
}

# The EXISTING Groww api-key SecureString (read-only reference; created OUTSIDE
# this stack). We reference it for its ARN — Terraform does not decrypt it.
data "aws_ssm_parameter" "api_key" {
  name            = "${var.param_prefix}/${var.api_key_param_name}"
  with_decryption = false
}

# The EXISTING Groww totp-secret SecureString (read-only reference).
data "aws_ssm_parameter" "totp_secret" {
  name            = "${var.param_prefix}/${var.totp_secret_param_name}"
  with_decryption = false
}

# --------------------------------------------------------------------------- #
# Locals — the canonical ARNs the least-privilege IAM policies scope to.
# --------------------------------------------------------------------------- #

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  token_param_full_name = "${var.param_prefix}/${var.token_param_name}"

  # SSM ParameterStore ARNs. SSM parameter ARNs drop the leading slash of the
  # name (arn:aws:ssm:<region>:<acct>:parameter/<name-without-leading-slash>).
  api_key_param_arn     = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/${var.api_key_param_name}"
  totp_secret_param_arn = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/${var.totp_secret_param_name}"
  token_param_arn       = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.token_param_full_name}"

  kms_key_arn = data.aws_kms_key.groww.arn
}

# --------------------------------------------------------------------------- #
# The shared token parameter — created here, WRITTEN by the Lambda at runtime.
# --------------------------------------------------------------------------- #

# `ignore_changes = [value]` because the Lambda overwrites the value on every
# mint; Terraform seeds a placeholder once and never fights the runtime writer.
resource "aws_ssm_parameter" "access_token" {
  name        = local.token_param_full_name
  description = "Shared Groww daily access token — SOLE writer is the token-minter Lambda (DEC-TOKEN-MINTER-CENTRAL-001)."
  type        = "SecureString"
  key_id      = data.aws_kms_key.groww.arn
  value       = "PLACEHOLDER_SET_BY_MINTER_LAMBDA"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

# --------------------------------------------------------------------------- #
# Lambda package — the handler PLUS its ONE third-party runtime dep (pyotp),
# bundled so the deployed Lambda actually IMPORTS at runtime.
#
# boto3 is already on the AWS-provided Lambda Python runtime, but pyotp is NOT —
# without it the handler's lazy `import pyotp` (TOTP computation) would
# ImportError on the first real mint. So a build step `pip install`s pyotp into a
# staging dir, copies the handler source alongside, and `archive_file` zips the
# whole staging tree into the deploy zip. pyotp is a pure-Python UNIVERSAL wheel
# (py3-none-any, zero deps), so the bundle has ZERO platform-specific artifacts
# and builds with NO Docker (DEC-MINTER-PURE-PYTHON-001, MIGRATION.md #1069). The
# token mint is a pure-stdlib HTTPS POST in handler.py, so growwapi (and its
# heavy native-extension transitive tree) is NOT bundled.
#
# `build_lambda_package.sh` is the ONE source of the packaging steps (also run
# by `deploy.sh` before apply). The `null_resource` re-runs it whenever the
# handler source or the pinned pyotp version changes (the `triggers` hashes), and
# the `archive_file` depends on it so the zip always contains a fresh build.
# --------------------------------------------------------------------------- #

locals {
  lambda_build_dir = "${path.module}/.build/package"
}

resource "null_resource" "build_lambda_package" {
  triggers = {
    handler_sha  = filesha256("${path.module}/handler.py")
    init_sha     = filesha256("${path.module}/__init__.py")
    build_sha    = filesha256("${path.module}/build_lambda_package.sh")
    dep_versions = var.pyotp_version
  }

  provisioner "local-exec" {
    command     = "bash build_lambda_package.sh"
    working_dir = path.module
    environment = {
      BUILD_DIR      = local.lambda_build_dir
      PYOTP_VERSION  = var.pyotp_version
      LAMBDA_RUNTIME = var.lambda_runtime
    }
  }
}

data "archive_file" "minter" {
  type        = "zip"
  source_dir  = local.lambda_build_dir
  output_path = "${path.module}/.build/minter.zip"

  depends_on = [null_resource.build_lambda_package]
}

# --------------------------------------------------------------------------- #
# (a) MINTER role — the ONLY principal that may read the two cred params AND
#     write the token param. Least-privilege: resource-scoped to the three
#     specific parameter ARNs + the one KMS key, never `*`.
# --------------------------------------------------------------------------- #

data "aws_iam_policy_document" "minter_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "minter" {
  name               = "${var.lambda_function_name}-minter"
  assume_role_policy = data.aws_iam_policy_document.minter_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "minter" {
  # Read the two EXISTING credential params (decrypted).
  statement {
    sid       = "ReadGrowwCredentials"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [local.api_key_param_arn, local.totp_secret_param_arn]
  }

  # Write the shared token param.
  statement {
    sid       = "WriteGrowwToken"
    effect    = "Allow"
    actions   = ["ssm:PutParameter"]
    resources = [local.token_param_arn]
  }

  # KMS: decrypt the cred SecureStrings on read + encrypt the token on write —
  # scoped to the ONE existing key.
  statement {
    sid       = "DecryptEncryptWithGrowwKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [local.kms_key_arn]
  }

  # Emit the TokenMintSuccess metric. PutMetricData does not support
  # resource-level scoping in IAM (resource must be `*`); the namespace is
  # constrained by the `cloudwatch:namespace` condition key instead.
  statement {
    sid       = "PutMintMetric"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.metric_namespace]
    }
  }
}

resource "aws_iam_role_policy" "minter" {
  name   = "${var.lambda_function_name}-minter"
  role   = aws_iam_role.minter.id
  policy = data.aws_iam_policy_document.minter.json
}

# CloudWatch Logs for the Lambda — scoped to this function's log group.
resource "aws_cloudwatch_log_group" "minter" {
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "minter_logs" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.minter.arn}:*"]
  }
}

resource "aws_iam_role_policy" "minter_logs" {
  name   = "${var.lambda_function_name}-logs"
  role   = aws_iam_role.minter.id
  policy = data.aws_iam_policy_document.minter_logs.json
}

# --------------------------------------------------------------------------- #
# The minter Lambda + its daily EventBridge schedule.
# --------------------------------------------------------------------------- #

resource "aws_lambda_function" "minter" {
  function_name = var.lambda_function_name
  role          = aws_iam_role.minter.arn
  handler       = "handler.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  # SERIALIZE overlapping invocations. A scheduled mint and a manual on-demand
  # invoke can otherwise run concurrently and race the token PutParameter:
  # mint(T1) -> mint(T2) -> put(T2) -> put(T1) persists the OLDER token T1. If
  # Groww invalidates the older session on a re-mint (UNVERIFIED, see ADR-0014
  # §3), readers would then auth-fail on the stale T1 until the next daily mint.
  # `reserved_concurrent_executions = 1` guarantees at most one invocation runs
  # at a time, so the two writes are strictly ordered (last-write-wins over a
  # SINGLE serialized writer is always the newest token) — this makes the Groww
  # session-model question moot for this stack (ADR-0014 §3).
  reserved_concurrent_executions = 1

  filename         = data.archive_file.minter.output_path
  source_code_hash = data.archive_file.minter.output_base64sha256
  tags             = var.tags

  environment {
    variables = {
      GROWW_API_KEY_PARAM              = data.aws_ssm_parameter.api_key.name
      GROWW_TOTP_SECRET_PARAM          = data.aws_ssm_parameter.totp_secret.name
      GROWW_TOKEN_PARAM                = aws_ssm_parameter.access_token.name
      GROWW_KMS_KEY_ID                 = data.aws_kms_key.groww.arn
      GROWW_METRIC_NAMESPACE           = var.metric_namespace
      GROWW_MINT_MAX_RETRIES           = tostring(var.mint_max_retries)
      GROWW_MINT_RETRY_BACKOFF_SECONDS = tostring(var.mint_retry_backoff_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.minter]
}

resource "aws_cloudwatch_event_rule" "daily_mint" {
  name                = "${var.lambda_function_name}-daily"
  description         = "Fire the Groww token minter daily, shortly after the 06:00 IST token reset."
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "daily_mint" {
  rule      = aws_cloudwatch_event_rule.daily_mint.name
  target_id = "groww-token-minter"
  arn       = aws_lambda_function.minter.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.minter.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_mint.arn
}

# --------------------------------------------------------------------------- #
# CloudWatch alarm + SNS topic — the operator is paged BEFORE a pull breaks.
# --------------------------------------------------------------------------- #

resource "aws_sns_topic" "alerts" {
  name = "${var.lambda_function_name}-alerts"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# STALE-TOKEN / FAILED-MINT alarm.
#
# This is a DAILY-JOB metric: the Lambda mints once/day (~00:35 UTC), so the
# TokenMintSuccess metric has exactly ONE real datapoint/day and ~23.5h/day with
# NO datapoint. The naive `treat_missing_data = "breaching"` over a continuous
# sliding-window evaluation is the classic daily-job anti-pattern — every period
# with a missing datum is padded "breaching", so the alarm FLAPS ALARM/OK every
# day on a perfectly HEALTHY pipeline.
#
# The AWS-recommended fix for a sparse/daily metric is metric math `FILL(m1, 0)`:
# it fills the missing datapoints in the window with 0 (an explicit "no success"),
# so the alarm evaluates a DENSE real series instead of padding-by-treatment. With
# a 24h SUM window:
#   * a genuine mint (TokenMintSuccess=1) in the window -> filled sum >= 1  -> OK
#   * NO success at all in the window — a wholly-dead/never-run Lambda (nothing
#     emitted, all periods fill to 0) OR every mint FAILING (each emits 0) — makes
#     the filled sum 0 < 1 -> ALARM.
# `treat_missing_data = "notBreaching"` now only governs the genuinely-empty
# bootstrap window before ANY data has ever been emitted (so a freshly-applied
# stack does not page before its first scheduled mint) — the FILL(0) series is
# what actually drives the verdict once the metric exists. The correct failure
# direction is PRESERVED (real 0.0 success -> fire; dead-Lambda silence -> fire),
# and the healthy-day flapping is eliminated.
#
# See ADR-0014 §"AWS API references" [3] (CloudWatch TreatMissingData) — AWS
# documents FILL(<metric>, <value>) metric math as the recommended treatment for
# a sparse/daily metric so missing periods read as an explicit value, not as the
# alarm's missing-data padding.
resource "aws_cloudwatch_metric_alarm" "stale_token" {
  alarm_name          = "${var.lambda_function_name}-stale-token"
  alarm_description   = "Groww token minter has not successfully minted within the expected window (stale token risk)."
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags

  # m1: the raw daily success metric (one real point/day, sparse).
  metric_query {
    id          = "m1"
    return_data = false
    metric {
      namespace   = var.metric_namespace
      metric_name = "TokenMintSuccess"
      period      = var.alarm_period_seconds
      stat        = "Sum"
    }
  }

  # e1: FILL the sparse metric's missing periods with 0 so the alarm evaluates a
  # dense series — a healthy day never pads "breaching", only a real absence of a
  # success (0 over the whole window) trips the LessThanThreshold(1) comparison.
  metric_query {
    id          = "e1"
    expression  = "FILL(m1, 0)"
    label       = "TokenMintSuccess (missing periods filled to 0)"
    return_data = true
  }
}

# FAILED-INVOCATION alarm: any Lambda Errors datum in a 6h window pages too, so
# a transient hard failure is surfaced immediately (not only at the daily
# stale-token window).
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.lambda_function_name}-errors"
  alarm_description   = "Groww token minter Lambda reported one or more invocation errors."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 21600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  dimensions = {
    FunctionName = aws_lambda_function.minter.function_name
  }
  tags = var.tags
}

# --------------------------------------------------------------------------- #
# (b) READER role — TickVault (live). READ the token param + KMS Decrypt ONLY.
#     No PutParameter, no cred-param read, no encrypt. Attach to the TickVault
#     compute principal (EC2/ECS/Lambda) out-of-band via its trust policy.
# --------------------------------------------------------------------------- #

data "aws_iam_policy_document" "reader_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type = "AWS"
      # Deliberately the account root as the assumable base — the operator
      # narrows this to the exact TickVault/BruteX compute principal ARNs when
      # wiring the reader roles to their workloads (documented in the runbook).
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

data "aws_iam_policy_document" "reader" {
  statement {
    sid       = "ReadGrowwToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [local.token_param_arn]
  }
  statement {
    sid       = "DecryptGrowwToken"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_role" "reader_tickvault" {
  name               = "${var.lambda_function_name}-reader-tickvault"
  assume_role_policy = data.aws_iam_policy_document.reader_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "reader_tickvault" {
  name   = "${var.lambda_function_name}-reader-tickvault"
  role   = aws_iam_role.reader_tickvault.id
  policy = data.aws_iam_policy_document.reader.json
}

# --------------------------------------------------------------------------- #
# (c) READER role — BruteX (backtest). Identical least-privilege as (b): READ
#     the token param + KMS Decrypt only. A separate role so the two consumers
#     can be audited + revoked independently.
# --------------------------------------------------------------------------- #

resource "aws_iam_role" "reader_brutex" {
  name               = "${var.lambda_function_name}-reader-brutex"
  assume_role_policy = data.aws_iam_policy_document.reader_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "reader_brutex" {
  name   = "${var.lambda_function_name}-reader-brutex"
  role   = aws_iam_role.reader_brutex.id
  policy = data.aws_iam_policy_document.reader.json
}

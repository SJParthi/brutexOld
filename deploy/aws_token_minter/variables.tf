# Input variables for the centralized Groww token-minter stack
# (DEC-TOKEN-MINTER-CENTRAL-001). Region, the SSM path prefix, the EventBridge
# schedule, and the SNS alert endpoint are all parameterized so the operator
# can deploy without editing HCL.

variable "region" {
  description = "AWS region to deploy into (the operator's existing infra is in Mumbai)."
  type        = string
  default     = "ap-south-1"
}

variable "param_prefix" {
  description = <<-EOT
    The SSM Parameter Store path prefix under which the Groww credentials and
    the shared access token live. The api-key + totp-secret SecureStrings ALREADY
    EXIST under this prefix (referenced via data sources, NOT created here); the
    token param <param_prefix>/access-token is created + written by this stack.
  EOT
  type        = string
  default     = "/tickvault/prod/groww"

  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]+[^/]$", var.param_prefix))
    error_message = "param_prefix must be an absolute SSM path with no trailing slash (e.g. /tickvault/prod/groww)."
  }
}

variable "api_key_param_name" {
  description = "SSM leaf name of the EXISTING Groww api-key SecureString (under param_prefix)."
  type        = string
  default     = "api-key"
}

variable "totp_secret_param_name" {
  description = "SSM leaf name of the EXISTING Groww totp-secret SecureString (under param_prefix)."
  type        = string
  default     = "totp-secret"
}

variable "token_param_name" {
  description = "SSM leaf name of the shared access token this stack writes (under param_prefix)."
  type        = string
  default     = "access-token"
}

variable "kms_key_alias" {
  description = <<-EOT
    Alias of the EXISTING KMS key that encrypts the Groww SecureStrings. Referenced
    via a data source (NOT created/managed here). Must include the `alias/` prefix.
  EOT
  type        = string
  default     = "alias/tickvault-groww"

  validation {
    condition     = startswith(var.kms_key_alias, "alias/")
    error_message = "kms_key_alias must start with 'alias/' (e.g. alias/tickvault-groww)."
  }
}

variable "schedule_expression" {
  description = <<-EOT
    EventBridge schedule expression for the daily mint. Default fires at
    00:35 UTC = 06:05 IST, shortly after the 06:00 IST Groww daily token reset.
    cron fields are: minute hour day-of-month month day-of-week year.
  EOT
  type        = string
  default     = "cron(35 0 * * ? *)"
}

variable "alert_email" {
  description = <<-EOT
    Email address the CloudWatch alarm notifies via SNS when the Lambda errors or
    has not successfully minted within the expected window. An empty string skips
    creating the email subscription (the operator can subscribe out-of-band, e.g.
    a chat webhook), but the SNS topic + alarm are always created.
  EOT
  type        = string
  default     = ""
}

variable "lambda_function_name" {
  description = "Name of the minter Lambda function."
  type        = string
  default     = "groww-token-minter"
}

variable "lambda_runtime" {
  description = "Python runtime for the minter Lambda (matches the project's 3.12 target)."
  type        = string
  default     = "python3.12"
}

variable "pyotp_version" {
  description = <<-EOT
    Pinned pyotp version bundled INTO the Lambda deploy zip by the build step.
    pyotp is NOT on the AWS Lambda runtime; the handler's lazy `import pyotp`
    (TOTP computation) needs it. It is the ONLY bundled third-party dep — a
    pure-Python UNIVERSAL wheel (py3-none-any, zero runtime deps), so the bundle
    has ZERO platform-specific artifacts and builds with no Docker
    (DEC-MINTER-PURE-PYTHON-001). The token mint is a pure-stdlib HTTPS POST in
    handler.py, so growwapi is NOT bundled. PINNED by default: 2.10.0 is the
    latest within BruteX's pyproject.toml `pyotp>=2.9,<3.0`. Use `pyotp` to float
    or `pyotp==X.Y.Z` to re-pin.
  EOT
  type        = string
  default     = "pyotp==2.10.0"
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Generous enough for the in-handler issuance-throttle retry/backoff."
  type        = number
  default     = 60
}

variable "lambda_memory_mb" {
  description = "Lambda memory size (MB). The mint is tiny; the floor is plenty."
  type        = number
  default     = 128
}

variable "mint_max_retries" {
  description = "In-handler retries for a transient Groww issuance throttle (429). Passed as GROWW_MINT_MAX_RETRIES."
  type        = number
  default     = 3
}

variable "mint_retry_backoff_seconds" {
  description = "Base seconds for the handler's exponential issuance-throttle backoff. Passed as GROWW_MINT_RETRY_BACKOFF_SECONDS."
  type        = number
  default     = 2
}

variable "metric_namespace" {
  description = "CloudWatch namespace for the TokenMintSuccess metric."
  type        = string
  default     = "BruteX/GrowwTokenMinter"
}

variable "alarm_period_seconds" {
  description = <<-EOT
    Evaluation period (seconds) for the stale-token / failed-invocation alarm.
    Default 24h so a single missed daily mint (a whole day with no success datum)
    trips the alarm's missing-data path.
  EOT
  type        = number
  default     = 86400
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every taggable resource this stack creates."
  type        = map(string)
  default = {
    Project   = "tickvault"
    Component = "groww-token-minter"
    ManagedBy = "terraform"
  }
}

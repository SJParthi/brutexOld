# Runbook — Centralized Groww token minter (AWS Lambda)

> **Decision:** `DEC-TOKEN-MINTER-CENTRAL-001` (ADR-0014, MIGRATION.md #1060).
> **Target audience:** the operator deploying + running the centralized Groww
> daily-token minter, and future maintainers auditing it.
> **Status:** the Lambda handler + Terraform IaC ship in this PR as CODE ONLY —
> **NOT deployed.** This session has no AWS access; the operator runs the deploy.

This runbook covers the exact paths, region, schedule, and one-command deploy for
the single-writer Groww access-token minter. Design rationale is in
`docs/adr/0014-centralized-token-minter.md`.

---

## 1. What it is

ONE AWS Lambda is the SOLE writer of the shared Groww daily access token. On an
EventBridge schedule (~06:05 IST, just after the 06:00 IST daily reset) and on an
on-demand manual invoke, it reads the api-key + totp-secret from Parameter Store,
computes the TOTP, mints a token via `GrowwAPI.get_access_token(...)`, and writes it
to a SecureString token param. TickVault (live) and BruteX (backtest) read that ONE
token READ-ONLY, so no application ever mints — the multi-app "minting storm" that
made the 06:00 IST reset flaky is eliminated by construction. Single-writer ⇒ NO
DynamoDB / no compare-and-swap.

## 2. Fixed facts (paths / region / schedule)

| Thing | Value |
|-------|-------|
| Region | `ap-south-1` (Mumbai) — `var.region` |
| SSM path prefix | `/tickvault/prod/groww` — `var.param_prefix` |
| EXISTING api-key param (referenced, not created) | `/tickvault/prod/groww/api-key` (SecureString) |
| EXISTING totp-secret param (referenced, not created) | `/tickvault/prod/groww/totp-secret` (SecureString) |
| Shared token param (created + written by the Lambda) | `/tickvault/prod/groww/access-token` (SecureString) |
| EXISTING KMS key (referenced by alias, not created) | `alias/tickvault-groww` — `var.kms_key_alias` |
| Schedule | `cron(35 0 * * ? *)` = 00:35 UTC = **06:05 IST** — `var.schedule_expression` |
| Lambda handler | `handler.lambda_handler` (`deploy/aws_token_minter/handler.py`) |
| Success metric | `TokenMintSuccess` in namespace `BruteX/GrowwTokenMinter` |

## 3. Prerequisites (operator, one-time)

1. The two credential SecureStrings already exist under the prefix (they do — this
   stack references them, never creates them).
2. The KMS key `alias/tickvault-groww` exists and encrypts those SecureStrings.
3. Terraform ≥ 1.5 + AWS credentials for the target account with permission to
   create the Lambda, IAM roles, EventBridge rule, CloudWatch alarms, SNS topic,
   and the ONE new token param.
4. The Lambda's runtime deps are **bundled automatically, cross-compiled for the
   Lambda's x86_64 Linux / cpython-3.12 runtime**: `boto3` is on the AWS Lambda
   Python runtime already; `growwapi` + `pyotp` are `pip install`ed INTO the deploy
   zip by `build_lambda_package.sh` (run by the Terraform
   `null_resource.build_lambda_package` build step, and by `./deploy.sh` before
   apply), so the deployed Lambda actually imports them at runtime. The install pins
   the Lambda's platform (`--platform manylinux2014_x86_64 --only-binary=:all:
   --python-version 3.12 --implementation cp --abi cp312`), so the bundle carries
   Linux-x86_64 wheels for the whole transitive tree **even when you build on an
   Apple-silicon / macOS ARM64 machine** (project rules §8) — no host-native binaries
   leak into the zip, so `from growwapi import GrowwAPI` cannot ImportError in the
   real Lambda. The build runs on YOUR deploy machine during `terraform apply` /
   `deploy.sh`, so `pip` + PyPI network access are needed there (a Python interpreter
   is used only as the pip driver — because the flags cross-target x86_64/cp312, the
   host interpreter's own arch does NOT have to be x86_64, but `python3.12` is still
   the default driver so it matches `var.lambda_runtime`). `--only-binary=:all:` means
   the build FAILS LOUDLY if a required manylinux x86_64 cp312 wheel is missing from
   PyPI, rather than silently shipping a broken zip. Pin exact versions with
   `-var 'growwapi_version=growwapi==X.Y.Z' -var 'pyotp_version=pyotp==A.B.C'` if
   desired (the defaults install the latest of each).

## 4. One-command deploy

The single command is **`./deploy.sh`** — it builds the bundle (handler + growwapi +
pyotp), runs `terraform init`, then `terraform apply`, which SHOWS the plan and
requires you to type `yes` (it never auto-approves):

```bash
cd deploy/aws_token_minter
./deploy.sh -var 'alert_email=you@example.com'
# or:  ALERT_EMAIL=you@example.com ./deploy.sh
```

Override any default by passing extra `-var` args straight through, e.g. a different
account's prefix or KMS alias:

```bash
./deploy.sh \
  -var 'region=ap-south-1' \
  -var 'param_prefix=/tickvault/prod/groww' \
  -var 'kms_key_alias=alias/tickvault-groww' \
  -var 'schedule_expression=cron(35 0 * * ? *)' \
  -var 'alert_email=you@example.com'
```

To run the steps by hand (or to schema-validate offline first):

```bash
cd deploy/aws_token_minter
bash build_lambda_package.sh  # bundle handler + growwapi + pyotp into the zip
terraform fmt -check          # formatting gate
terraform init                # fetch the aws + archive + null providers
terraform validate            # schema-level validation (needs the providers)
terraform apply -var 'alert_email=you@example.com'   # review plan, type 'yes'
```

After `apply`, **confirm the SNS email subscription** (AWS emails a confirmation
link to `alert_email`; the alarm cannot page until you click it).

## 5. On-demand mint (manual invoke)

```bash
aws lambda invoke \
  --region ap-south-1 \
  --function-name "$(terraform output -raw minter_lambda_name)" \
  /dev/stdout
```

A success prints `{"ok": true, "attempts": N, "token_param": ".../access-token"}`
and the token param is overwritten with a fresh value.

## 6. Wiring the readers (least-privilege)

Terraform creates two reader roles — `reader-tickvault` and `reader-brutex` — each
with `ssm:GetParameter` on the token param + `kms:Decrypt` on the key, and NOTHING
else. Attach each to its workload's compute principal and narrow the role's trust
policy to that principal's ARN:

```bash
terraform output reader_tickvault_role_arn
terraform output reader_brutex_role_arn
```

The BruteX read-side code change (point the adapter at the centralized token param
instead of minting) is a SEPARATE PR — this runbook covers the minter + IaC only.

## 7. Alarms

- **`<fn>-stale-token`** — the `TokenMintSuccess` metric must sum to ≥ 1 within the
  window (default 24h). It uses metric math `FILL(m1, 0)` (missing periods filled to
  0) with `treat_missing_data = "notBreaching"`, so a HEALTHY day (one success in the
  window) reads OK and does NOT flap — only a genuine absence of a success in the
  window (a wholly-dead/never-run Lambda, or every mint failing so it emits 0) makes
  the filled sum 0 < 1 and pages you. This is the daily-job missing-data treatment
  (a plain `treat_missing_data = "breaching"` would flap ALARM/OK every day because
  the metric is sparse — one point/day). See ADR-0014
  §"Stale-token alarm — daily-job missing-data treatment".
- **`<fn>-errors`** — any `AWS/Lambda Errors` datum in a 6h window pages immediately,
  so a hard failure surfaces before the daily stale-token window.

Both publish to the SNS topic `terraform output alerts_topic_arn`.

## 8. Rollback

```bash
cd deploy/aws_token_minter
terraform destroy -var 'alert_email=you@example.com'
```

`destroy` removes the Lambda, schedule, alarms, SNS topic, the 3 IAM roles, and the
token param (the `access-token` SecureString). It does **NOT** touch the EXISTING
credential params or the KMS key (they are `data` sources). With the minter gone,
each application falls back to its in-process auto-re-mint (DEC-GROWW-TOKEN-REAUTH-001
#909) until the minter is re-applied.

## 9. Secret hygiene

The handler scrubs the api-key, the TOTP secret, and the minted 6-digit code to
`***` before any CloudWatch log line (`_sanitize`). No secret is ever logged. The
token itself is written only to the SecureString param (KMS-encrypted at rest), never
logged.

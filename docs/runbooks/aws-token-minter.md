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
computes the TOTP (`pyotp`), mints a token via a pure-stdlib HTTPS POST to the Groww
token endpoint (`handler.py::get_groww_access_token`, reimplementing the request +
token-extraction contract of `growwapi` 1.5.0 `GrowwAPI.get_access_token` with simpler
structural error handling — no `growwapi` dependency, no
Docker), and writes it to a SecureString token param. TickVault (live) and BruteX
(backtest) read that ONE
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
4. The Lambda's runtime dep is **bundled automatically — pure Python, NO Docker**
   (DEC-MINTER-PURE-PYTHON-001): `boto3` is on the AWS Lambda Python runtime already;
   `pyotp` (the ONLY bundled dep) is `pip install`ed INTO the deploy zip by
   `build_lambda_package.sh` (run by the Terraform `null_resource.build_lambda_package`
   build step, and by `./deploy.sh` before apply), so the deployed Lambda actually
   imports it at runtime. The Groww token is minted by a **pure-stdlib HTTPS POST** in
   the handler (`get_groww_access_token`, reimplementing the request +
   token-extraction contract of `growwapi` 1.5.0 `GrowwAPI.get_access_token`,
   `client.py:1540-1580`, with simpler structural error handling), so `growwapi`
   — and its heavy native-extension transitive tree (`pynacl`, `pydantic-core`,
   `pandas`, the sdist-only `clint`) — is NOT bundled.

   **No Docker, no cross-compile flags, no two-path build.** `pyotp==2.10.0` is a
   pure-Python **universal** wheel (`py3-none-any`, zero runtime deps), so the bundle
   has ZERO platform-specific artifacts and builds identically on macOS/ARM64 (project
   rules §8) and Linux with one deterministic `pip install --target`. This SUPERSEDES
   the two-path Docker/cross-pip build of DEC-MINTER-BUILD-ROBUST-001 — with `growwapi`
   (and thus `clint`) gone, the operator's `ResolutionImpossible` cannot recur. `pyotp`
   is **PINNED** by default (`pyotp==2.10.0`, the latest within BruteX's own
   `pyproject.toml` `pyotp>=2.9,<3.0`); re-pin with `-var 'pyotp_version=pyotp==A.B.C'`
   (or pass a bare `pyotp` to float). `deploy/aws_token_minter/requirements.txt`
   declares the one dep.

   The build runs on YOUR deploy machine during `terraform apply` / `deploy.sh`, so
   only `pip` + PyPI network access is needed there — **no Docker daemon**.

## 4. One-command deploy

The single command is **`./deploy.sh`** — it builds the bundle (handler + pyotp, pure
Python, no Docker), runs `terraform init`, auto-adopts any pre-existing name-fixed
resources into the remote state (`terraform import`, idempotent —
DEC-DEPLOY-AUTO-FIRST-MINT-001), then `terraform apply`, which SHOWS the
plan and requires you to type `yes` (it never auto-approves), and finally triggers
the **first token mint itself** (one `aws lambda invoke`, verified `ok: true`,
honest `FIRST MINT: PASS`/`FAIL`, non-zero exit on failure) so the token param is
live immediately — no manual invoke, no empty-token window until the next
06:05 IST cron:

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
bash build_lambda_package.sh  # bundle handler + pyotp into the zip (pure Python, no Docker)
terraform fmt -check          # formatting gate
terraform init                # fetch the aws + archive + null providers
terraform validate            # schema-level validation (needs the providers)
terraform apply -var 'alert_email=you@example.com'   # review plan, type 'yes'
```

After `apply`, **confirm the SNS email subscription** (AWS emails a confirmation
link to `alert_email`; the alarm cannot page until you click it).

## 5. On-demand mint (manual invoke)

> You normally never need this: `./deploy.sh` performs the FIRST mint itself right
> after a successful apply (step 6/6, DEC-DEPLOY-AUTO-FIRST-MINT-001) and fails the
> deploy loudly (non-zero exit) if that mint did not report `ok: true`. Use the
> command below only for an ad-hoc re-mint — it is idempotent (the Lambda is the
> sole token writer; the SSM write is last-writer-wins `Overwrite=True`).

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
instead of minting) LANDED as PR #1015 / DEC-TOKEN-BROKER-AWS-001 (MIGRATION.md
#1064) and was then made the DEFAULT AND ONLY credential/token source — no flag, no
local-mint fallback — by DEC-AWS-ONLY-CREDS-001 (MIGRATION.md #1065). This runbook
covers the minter + IaC only; the read side is `docs/runbooks/aws-token-broker.md`.

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
**production BruteX does NOT fall back to a local mint — it fails LOUD**: the
read-only cold start bounded-retries the SSM read, then raises a `GrowwAuthError`
naming `/tickvault/prod/groww/access-token` + the deploy pointer
`deploy/aws_token_minter/deploy.sh` (DEC-REAUTH-COLDSTART-READONLY-001, MIGRATION.md
#1092); a mid-run stale token likewise RE-READS + bounded-waits, never mints
(DEC-BROKER-READONLY-REAUTH-REREAD-001 #1072). The in-process auto-re-mint
(DEC-GROWW-TOKEN-REAUTH-001 #909) survives ONLY for explicitly non-read-only
local-dev configs (CLAUDE.md §6), never the production read-only broker. TickVault's
rollback behavior is owned by its own repo — not asserted here. Remediation:
re-apply the minter (`./deploy.sh`).

## 9. Secret hygiene

The handler scrubs the api-key, the TOTP secret, and the minted 6-digit code to
`***` before any CloudWatch log line (`_sanitize`). No secret is ever logged. The
token itself is written only to the SecureString param (KMS-encrypted at rest), never
logged.

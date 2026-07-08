# Runbook — Shared AWS Parameter-Store token broker (DEC-TOKEN-BROKER-AWS-001)

> **✅ Reconciled (2026-07-06, DEC-BROKER-DOCS-AWSONLY-RECONCILE-001):** this is the
> "full rewrite of this runbook" the 2026-07-03 reconcile banner named as a follow-up.
> The `BRUTEX_TOKEN_BROKER_READONLY` opt-in flag and the "degrade to the local disk
> cache + local mint" fallback this runbook once taught were REMOVED by
> DEC-AWS-ONLY-CREDS-001 (MIGRATION.md #1065): AWS Parameter Store is the DEFAULT AND
> ONLY credential/token source, and the broker read is unconditional + **fail-fast** —
> no env fallback, no file fallback, no local mint, no opt-in flag. Primary code
> anchor: the `brutex/adapters/groww/token_store.py:8-10` module docstring (quotes the
> removal verbatim); secondary: the "Shared token broker" constants block in
> `brutex/adapters/groww/constants.py` (DEC-TOKEN-BROKER-AWS-001 /
> DEC-AWS-ONLY-CREDS-001 comment, currently at :471-482).

BruteX reads the Groww creds **and** the daily access token from the shared AWS Systems
Manager **Parameter Store** — the same place TickVault reads. A **single dedicated
Lambda mints** the shared access token (DEC-TOKEN-MINTER-CENTRAL-001, MIGRATION.md
#1060; `docs/runbooks/aws-token-minter.md`); BruteX and TickVault are strictly
**READ-ONLY** consumers. The language-agnostic interop contract is
`docs/TOKEN_BROKER_CONTRACT.md`.

**This is the ONLY mode — there is no local mode.** The read is unconditional
(`token_store.py::resolve_token_store` — "no `BRUTEX_TOKEN_BROKER_READONLY` gate and no
local-mint fallback. This is FAIL-FAST", :109-117) and a live pull without working AWS
access fails LOUD by design (Golden Rule #1: a token comes from AWS or the pull fails
LOUD). Local dev / CI without AWS credentials cannot run a live pull — tests run against
the injected in-memory fakes instead.

The creds already exist in the operator's account (region `ap-south-1`):
`/tickvault/prod/groww/api-key` and `/tickvault/prod/groww/totp-secret`. This runbook
covers the shared access-token parameter and BruteX's read side.

## 0. Install the required extra (BruteX host only)

```bash
pip install -e ".[token-broker]"   # adds boto3; NOT installed by bootstrap.sh
```

Under AWS-only this extra is **required for any live pull**: a missing boto3 raises a
clear `GrowwAuthError` naming the deploy pointer (`token_store.py::
_build_readonly_store_or_raise`, :128-149) — never a silent fall back to a local mint.

## 1. The three shared SecureString parameters (region ap-south-1)

The api_key + TOTP secret already exist; confirm them and create the access-token
parameter that the Lambda keeps minted. All three are SecureStrings (SSE-KMS at rest).

```bash
# Already present (operator's existing infra) — confirm:
aws ssm get-parameter --name /tickvault/prod/groww/api-key     --with-decryption --region ap-south-1 >/dev/null
aws ssm get-parameter --name /tickvault/prod/groww/totp-secret --with-decryption --region ap-south-1 >/dev/null

# The shared access token — the Lambda is the ONLY writer; seed an empty placeholder once:
aws ssm put-parameter --name /tickvault/prod/groww/access-token \
  --type SecureString --region ap-south-1 --value 'pending-first-mint'
```

> The Lambda minter + its Terraform SHIP at `deploy/aws_token_minter/`
> (DEC-TOKEN-MINTER-CENTRAL-001, MIGRATION.md #1060; operator walkthrough
> `docs/runbooks/aws-token-deploy-guide.md`). Until the minter is deployed and has
> minted, a live pull fails LOUD naming that deploy pointer — there is no local-mint
> stopgap (DEC-AWS-ONLY-CREDS-001 #1065).

## 2. Least-privilege IAM policy (read-only — BruteX host / instance role)

BruteX only **reads** the three parameters and needs KMS **decrypt** for the SecureStrings.
It never writes SSM and never encrypts.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ReadGrowwParams", "Effect": "Allow",
      "Action": ["ssm:GetParameter"],
      "Resource": "arn:aws:ssm:ap-south-1:<acct>:parameter/tickvault/prod/groww/*" },
    { "Sid": "DecryptSecureString", "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "arn:aws:kms:ap-south-1:<acct>:key/<securestring-cmk-id>" }
  ]
}
```

Only the `ssm:GetParameter` + `kms:Decrypt` actions are granted — no `PutParameter`, no
`kms:Encrypt` — enforcing read-only at the IAM boundary (defence-in-depth on top of the
code's `TokenBrokerReadOnlyError`, `brutex/adapters/groww/token_store_aws.py`).

## 3. Environment (BruteX host)

There is **no enable flag**. The only surviving env vars are NON-SECRET overrides of the
SSM parameter paths / region (`constants.py`, "Shared token broker" block; model table in
`docs/runbooks/ENV_VARS.md` §5):

```bash
BRUTEX_TOKEN_AWS_REGION=ap-south-1          # optional; ap-south-1 is the default
# Optional path overrides (defaults shown — usually unnecessary):
# BRUTEX_TOKEN_PARAM_ACCESS_TOKEN=/tickvault/prod/groww/access-token
# BRUTEX_TOKEN_PARAM_API_KEY=/tickvault/prod/groww/api-key
# BRUTEX_TOKEN_PARAM_TOTP_SECRET=/tickvault/prod/groww/totp-secret
# plus standard AWS creds: AWS_PROFILE / instance role / AWS_ACCESS_KEY_ID+SECRET
```

Region resolution: `BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → the `ap-south-1` default.

**Fail-FAST (never fail-safe — DEC-AWS-ONLY-CREDS-001 #1065):**

- **SSM store build failure** (missing boto3 extra / unbuildable client / bad region) →
  `GrowwAuthError` naming the deploy pointer `deploy/aws_token_minter/deploy.sh`
  (`token_store.py::_build_readonly_store_or_raise`, :128-149) — never a mid-pull
  surprise, never a local mint.
- **Credential read failure** (absent/blank parameter, IAM/KMS/network error) →
  `CredentialsError` telling the operator exactly what to fix
  (`brutex/config/credentials_aws.py:1-27` — "NEVER falls back to env vars, a local
  file, or the removed webpage"; both fields or a loud error).
- **Mid-run stale token** (the 06:00-IST reset) → BruteX RE-READS the Lambda-minted
  SecureString and ADOPTS it (log `groww.token.reauth` then `groww.token.reread` —
  DEC-BROKER-READONLY-REAUTH-REREAD-001, MIGRATION.md #1072), with a bounded
  single-flight stale-wait pause while the Lambda's ~06:05 mint lands
  (DEC-TOKEN-STALE-PAUSE-001 #1190); a still-stale read raises the RETRYABLE
  `GrowwTokenStillStaleError` (the window quarantines + re-pulls) —
  `brutex/adapters/groww/reauth.py:79-129`.
- **Cold start with no readable token** → bounded read retries, then a LOUD actionable
  error naming `/tickvault/prod/groww/access-token` + the deploy pointer — the mint call
  is UNREACHABLE on the read-only broker (DEC-REAUTH-COLDSTART-READONLY-001 #1092).

## 4. Operations

- **Rotate the api_key or TOTP secret:** overwrite the SecureString(s) in Parameter Store.
  Both apps pick up the new value on their next resolve.
- **The access token:** the Lambda re-mints it (~06:05 IST, after the 06:00 IST reset).
  BruteX picks up the re-minted token **MID-RUN** via the read-only reauth re-read +
  stale-wait pause (#1072/#1190) — no process restart needed.
- **Read-only fail-fast:** BruteX NEVER mints/writes the shared token. If the shared
  access token is absent/blank when a fresh token would be needed, BruteX raises LOUD
  (cold start: the bounded-read-retries-then-`GrowwAuthError` path of #1092; a store
  attempt: `TokenBrokerReadOnlyError`) rather than silently minting a local token.
- **Remediation for a dead/missing token:** deploy or re-run the Lambda minter
  (`deploy/aws_token_minter/deploy.sh`) — **never a BruteX-side mint**. (The pre-#1065
  "revert to local by unsetting the flag" escape hatch no longer exists.)

## 5. What is shared

Everything. The pre-#1065 "Local (disk fallback / default)" column of this table — an
on-disk token cache, a local TOTP mint, and an on-disk credentials file — was REMOVED
(DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065; historical record: ADR-0013 §4 +
`git log` of this runbook):

| | Shared (AWS Parameter Store, ap-south-1) |
|---|---|
| Daily token | `/tickvault/prod/groww/access-token` (SecureString, minted by the Lambda, read-only) |
| api_key | `/tickvault/prod/groww/api-key` (SecureString) |
| TOTP secret | `/tickvault/prod/groww/totp-secret` (SecureString) |

A read is a plain `GetParameter WithDecryption=True` (KMS-decrypted server-side). BruteX
performs **no writes** — read-only is the only mode.

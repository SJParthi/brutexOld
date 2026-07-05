# Runbook — Shared AWS Parameter-Store token broker (DEC-TOKEN-BROKER-AWS-001)

> **⚠️ Reconcile banner (DEC-AWS-ONLY-CREDS-001, 2026-07-03):** the sections below that
> describe the `BRUTEX_TOKEN_BROKER_READONLY` opt-in flag and the "degrade to the local
> disk cache + local mint" fallback are STALE — both were REMOVED
> (`brutex/adapters/groww/constants.py:348-350`): AWS is now the DEFAULT AND ONLY
> credential/token source and the broker read is unconditional + **fail-fast** (no env
> fallback, no file fallback, no local mint, no opt-in flag). The SSM paths / region /
> IAM sections stay correct. A full rewrite of this runbook is a named follow-up.

Point BruteX at the shared AWS Systems Manager **Parameter Store** so it reads the Groww
creds **and** the daily access token from **one place** — the same place TickVault reads.
A **single dedicated Lambda mints** the shared access token; BruteX and TickVault are
strictly **READ-ONLY** consumers. The language-agnostic interop contract is
`docs/TOKEN_BROKER_CONTRACT.md`.

**Default is LOCAL — you never need any of this for a laptop-only run.** With
`BRUTEX_TOKEN_BROKER_READONLY` unset, BruteX uses the on-disk token cache + local TOTP mint
(byte-identical to DEC-GROWW-TOKEN-DISK-CACHE-001) and never imports boto3. Do this ONLY to
share the token across two apps.

The creds already exist in the operator's account (region `ap-south-1`):
`/tickvault/prod/groww/api-key` and `/tickvault/prod/groww/totp-secret`. This runbook adds the
shared access-token parameter and points BruteX at all three.

## 0. Install the optional extra (BruteX host only)

```bash
pip install -e ".[token-broker]"   # adds boto3; NOT installed by bootstrap.sh
```

## 1. The three shared SecureString parameters (region ap-south-1)

The api_key + TOTP secret already exist; confirm them and create the access-token parameter
that the Lambda will keep minted. All three are SecureStrings (SSE-KMS at rest).

```bash
# Already present (operator's existing infra) — confirm:
aws ssm get-parameter --name /tickvault/prod/groww/api-key     --with-decryption --region ap-south-1 >/dev/null
aws ssm get-parameter --name /tickvault/prod/groww/totp-secret --with-decryption --region ap-south-1 >/dev/null

# The shared access token — the Lambda is the ONLY writer; seed an empty placeholder once:
aws ssm put-parameter --name /tickvault/prod/groww/access-token \
  --type SecureString --region ap-south-1 --value 'pending-first-mint'
```

> The Lambda minter + its Terraform are **out of scope for this PR** — a separate PR owns
> `deploy/aws_token_minter/`. Until it is deployed, keep `BRUTEX_TOKEN_BROKER_READONLY` unset
> (local mint), or mint once by hand and put the value into the access-token parameter.

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
code's `TokenBrokerReadOnlyError`).

## 3. Environment (BruteX host)

```bash
BRUTEX_TOKEN_BROKER_READONLY=true
BRUTEX_TOKEN_AWS_REGION=ap-south-1          # optional; ap-south-1 is the default
# Optional path overrides (defaults shown — usually unnecessary):
# BRUTEX_TOKEN_PARAM_ACCESS_TOKEN=/tickvault/prod/groww/access-token
# BRUTEX_TOKEN_PARAM_API_KEY=/tickvault/prod/groww/api-key
# BRUTEX_TOKEN_PARAM_TOTP_SECRET=/tickvault/prod/groww/totp-secret
# plus standard AWS creds: AWS_PROFILE / instance role / AWS_ACCESS_KEY_ID+SECRET
```

Region resolution: `BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → the `ap-south-1` default.

**Fail-safe:** a bad/blank `BRUTEX_TOKEN_BROKER_READONLY`, a missing boto3, or an unbuildable
SSM client all degrade to the **local disk cache + local mint** at build time (a loud
`groww.token.aws_*` log), never a mid-pull crash. Every SSM read error (unreachable / IAM /
region / KMS) degrades the token read to a miss, never a raise.

## 4. Operations

- **Rotate the api_key or TOTP secret:** overwrite the SecureString(s) in Parameter Store.
  Both apps pick up the new value on their next resolve.
- **The access token:** the Lambda re-mints it (default 06:00 IST reset). BruteX reads the
  current value; it picks up a re-minted token on its next cold-start (process restart).
- **Read-only fail-fast:** on a read-only host BruteX NEVER mints/writes the shared token. If
  the shared access token is absent/blank when a fresh mint would be needed, BruteX raises
  `TokenBrokerReadOnlyError` (loud) rather than silently minting a local token — ensure the
  Lambda has minted the token, or unset `BRUTEX_TOKEN_BROKER_READONLY` to mint locally.
- **Revert to local:** unset `BRUTEX_TOKEN_BROKER_READONLY` (or set it to `0`/`false`).
  Instant, no code change, byte-identical to the pre-broker behaviour.

## 5. What is shared vs local

| | Shared (AWS Parameter Store, ap-south-1) | Local (disk fallback / default) |
|---|---|---|
| Daily token | `/tickvault/prod/groww/access-token` (SecureString, minted by the Lambda, read-only) | minted locally + `~/.brutex/cache/groww_access_token.json` (0600) |
| api_key | `/tickvault/prod/groww/api-key` (SecureString) | `~/.brutex/credentials.json` (0600) or env |
| TOTP secret | `/tickvault/prod/groww/totp-secret` (SecureString) | `~/.brutex/credentials.json` (0600) or env |

A read is a plain `GetParameter WithDecryption=True` (KMS-decrypted server-side). BruteX
performs **no writes** in read-only mode.

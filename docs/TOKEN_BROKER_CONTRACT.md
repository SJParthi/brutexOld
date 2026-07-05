# Shared AWS Parameter-Store Token-Broker Contract (DEC-TOKEN-BROKER-AWS-001)

> **⚠️ Reconcile banner (DEC-AWS-ONLY-CREDS-001, 2026-07-03):** the "Enable flag"
> (`BRUTEX_TOKEN_BROKER_READONLY`) + "LOCAL disk cache + local mint" clauses below are
> STALE — both were REMOVED (`brutex/adapters/groww/constants.py:348-350`): the broker
> read is now unconditional + fail-fast for every consumer (AWS is the SOLE
> credential/token source). The parameter paths, read/write conventions and IAM contract
> stay correct. A full contract-doc refresh is a named follow-up.

> **Status:** authoritative interop spec. **Owner:** BruteX. **Consumers:** BruteX
> (Python, `brutex/adapters/groww/token_store_aws.py` + `brutex/config/credentials_aws.py`)
> **and** TickVault (its own repo) — both READ-ONLY — plus a single dedicated **Lambda
> minter** (a separate PR owns `deploy/aws_token_minter/`) that is the ONLY writer. This
> document is the language-agnostic contract every party MUST agree on so the shared store
> works. Writing/reviewing the Lambda or TickVault code is **out of scope** here.

## 1. Why this exists

The Groww daily access token is minted once per session and cached. That cache is
**per-machine**, so two apps on **one Groww account** (TickVault live feed, BruteX
backtesting) each mint their own token. Groww's single-active-token behaviour means a mint
by one app silently **revokes** the other's live token — the "TickVault live feed
auth-rejected after a deploy" incident.

The operator's design: put the **creds** (api_key + TOTP secret) **and** the **daily access
token** in **one shared place** — AWS Systems Manager **Parameter Store** in region
`ap-south-1` — and make **exactly one dedicated Lambda** the minter/writer of the access
token. TickVault and BruteX are strictly **READ-ONLY** consumers. So:

- a **key rotation** in Parameter Store propagates to both apps on their next resolve;
- both apps **read the same token** (the load-bearing fix), so a deploy/restart reuses the
  Lambda's token instead of minting a new one that revokes the sibling;
- because there is **exactly one writer**, there is **no compare-and-swap / DynamoDB /
  write-contention** problem — a plain SecureString parameter is sufficient.

**Honesty envelope (Golden Rule #10):** the single-active-token vendor behaviour is
**UNVERIFIED** against Groww docs (the SDK reference labels the TOTP flow "No Expiry"; the
06:00-IST reset is asserted from production logs + ADR-0009, not vendor docs). The broker is
correct **regardless** — one shared token read by both apps is strictly better than N
independent mints whether or not the vendor enforces single-active-token.

## 2. The three shared parameters (SecureStrings, region ap-south-1)

All three are **SecureStrings** (encrypted at rest with a KMS key; SSE handled by SSM). The
paths below are the defaults; each is env-overridable on the reader (§5).

| Purpose | Parameter path (default) | Writer | Readers |
|---|---|---|---|
| Daily access token | `/tickvault/prod/groww/access-token` | **Lambda only** | TickVault, BruteX |
| Groww api_key | `/tickvault/prod/groww/api-key` | operator (rotation) | TickVault, BruteX |
| Groww TOTP secret | `/tickvault/prod/groww/totp-secret` | operator (rotation) | TickVault, BruteX |

The api_key + TOTP-secret parameters **already exist** in the operator's account. This
contract adds the shared **access-token** parameter.

## 3. Read convention

Every reader (BruteX, TickVault) reads a parameter with:

```
GetParameter(Name=<path>, WithDecryption=true)
```

`WithDecryption=true` makes SSM **KMS-decrypt the SecureString server-side** and return the
plaintext value (AWS SSM docs, *"Working with SecureString parameters"* —
<https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-securestring.html>;
`GetParameter` API reference —
<https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetParameter.html>).
The reader holds no KMS key material and performs no client-side crypto.

Validation + fail-safe (BruteX: `token_store_aws.py::ParameterStoreTokenStore.load` /
`credentials_aws.py::load_groww_aws_credentials`):

1. A non-blank returned value is the token / cred. A **blank or absent** value is a **miss**.
2. **Any raise** (network / IAM / region / KMS / `ParameterNotFound`) is caught and degrades
   to a **miss** — the read NEVER raises into the caller. A miss degrades BruteX to its local
   disk cache + local mint (fail-safe).
3. Creds are **both fields or nothing**: if either the api_key or the TOTP-secret parameter
   is absent/blank, the reader returns "no AWS creds" and falls back to the local file for a
   coherent pair — never a mixed AWS-key / file-secret credential.

## 4. Write / mint convention — single writer, read-only consumers

**The Lambda is the ONLY writer of the access token.** It mints the daily Groww token and
`PutParameter`s it into `/tickvault/prod/groww/access-token` (overwrite; last-write-wins, but
with a single writer there is no contention). It handles the daily 06:00-IST reset.

**Consumers (BruteX, TickVault) MUST NOT write / mint the shared token.** In BruteX this is
enforced two ways:

- **In code:** `ParameterStoreTokenStore.store` raises `TokenBrokerReadOnlyError` (a terminal
  `GrowwAuthError`) rather than writing — so a mint attempt is a **loud fail-fast**, never a
  silent local write that would fork the shared token. (BruteX picks up the Lambda's
  re-minted token on its next cold-start / process restart.)
- **At the IAM boundary:** the reader role is granted only `ssm:GetParameter` + `kms:Decrypt`
  — no `PutParameter`, no `kms:Encrypt` (see the runbook §2).

## 5. Region + path resolution

- **Region:** `BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → the `ap-south-1` default (a concrete
  region is always used; a mis-set profile default cannot silently read the wrong region).
- **Enable flag:** `BRUTEX_TOKEN_BROKER_READONLY` truthy (`1`/`true`/`yes`/`on`,
  case-insensitive) enables the read-only broker; anything else ⇒ the LOCAL disk cache +
  local mint (byte-identical to the pre-broker behaviour).
- **Paths:** each defaults to `/tickvault/prod/groww/{access-token,api-key,totp-secret}` and
  is overridable via `BRUTEX_TOKEN_PARAM_ACCESS_TOKEN` / `BRUTEX_TOKEN_PARAM_API_KEY` /
  `BRUTEX_TOKEN_PARAM_TOTP_SECRET`.

## 6. Rotation

- **api_key / TOTP secret:** overwrite the SecureString; both apps read the new value on
  their next resolve. No consumer writes creds (the operator provisions/rotates).
- **Access token:** the Lambda re-mints on the 06:00-IST reset. Consumers read the current
  value and pick up a re-minted token on their next cold-start.

## 7. In-memory secret exposure (honest disclosure)

`ssm:GetParameter WithDecryption=true` gives every reader host the decrypted cred/token in
memory — the same threat surface as today's on-disk secret, so **no regression**, but it is a
**new** N-host exposure vs a single machine. Bounded + acceptable, disclosed here. In
read-only mode BruteX reads the **token** (the api_key/TOTP secret are read only when a local
mint is needed, i.e. when the broker is disabled).

## 8. Operator provisioning (minimal, least-privilege)

See `docs/runbooks/aws-token-broker.md` for the copy-paste setup. In brief:

1. The three SecureString parameters under `/tickvault/prod/groww/` (api_key + totp_secret
   already exist; add the access-token parameter — the Lambda keeps it minted).
2. A **read-only** least-privilege IAM policy on the consumer role: `ssm:GetParameter` scoped
   to `parameter/tickvault/prod/groww/*` + `kms:Decrypt` on the SecureString CMK. No
   `PutParameter`, no `kms:Encrypt`.
3. The env on each consumer host (`BRUTEX_TOKEN_BROKER_READONLY=true`, optionally the region /
   path overrides).

The Lambda minter's IAM (with `ssm:PutParameter` + `kms:Encrypt`) and its Terraform are owned
by the separate `deploy/aws_token_minter/` PR — out of scope for this contract's read side.

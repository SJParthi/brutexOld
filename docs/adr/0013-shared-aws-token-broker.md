# ADR-0013 — Shared AWS Parameter-Store token broker for the daily Groww token + creds

**Decision ID:** `DEC-TOKEN-BROKER-AWS-001` (MIGRATION.md entry #1064)
**Date:** 2026-07-01
**Status:** ACCEPTED — BruteX READ-side implemented; a separate PR owns the Lambda minter (`deploy/aws_token_minter/`); TickVault reads the same parameters (its code is in its own repo). SUPERSEDED-IN-PART (2026-07-06, DEC-BROKER-DOCS-AWSONLY-RECONCILE-001): the §4 opt-in flag (`BRUTEX_TOKEN_BROKER_READONLY`), local-mint fallback, and cold-start-only pickup consequences were superseded by DEC-AWS-ONLY-CREDS-001 (MIGRATION.md #1065) + DEC-BROKER-READONLY-REAUTH-REREAD-001 (#1072) + DEC-REAUTH-COLDSTART-READONLY-001 (#1092); body preserved as the historical record.
**Author:** operator-directed
**Builds on:** DEC-GROWW-TOKEN-DISK-CACHE-001 (#1041 — the per-machine on-disk token cache + the injected `token_cache_load`/`token_cache_store` seam on `SdkRefresher`), DEC-GROWW-TOKEN-REAUTH-001 (#909 — the cache-first cold-start + forced re-mint), DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009 (the Groww vendor).

## 1. Context

The daily Groww access token is minted once per session and cached on local disk
(`~/.brutex/cache/groww_access_token.json`, 0600). The `token_cache.py` docstring flags the
gap this ADR closes: *"The cache is PER-MACHINE, so a Groww-side ACCOUNT-WIDE issuance limit
shared across many hosts is NOT solved here (that needs a networked token broker — out of
scope, not claimed)."*

Two apps run on **one Groww account**: TickVault (live feed, AWS) and BruteX (backtesting).
Each mints its own token; Groww's single-active-token behaviour means a mint by one
**revokes** the other's live token — the operator's "TickVault live feed auth-rejected after
a deploy" incident.

The operator's **existing** infrastructure already holds the Groww credentials in **AWS
Systems Manager Parameter Store** in region `ap-south-1` as SecureStrings —
`/tickvault/prod/groww/api-key` and `/tickvault/prod/groww/totp-secret`. The operator's FINAL
design: the shared **access token** ALSO lives in Parameter Store at
`/tickvault/prod/groww/access-token`, and **a single dedicated Lambda is the ONLY writer /
minter** of that token. Both TickVault and BruteX are strictly **READ-ONLY** consumers —
"only one place for both TickVault live and BruteX backtesting."

**Honesty envelope (Golden Rule #10):** the single-active-token vendor behaviour is
**UNVERIFIED** (the SDK reference labels the TOTP flow "No Expiry"; the 06:00-IST reset is
asserted from production logs + ADR-0009, not vendor docs). The single-writer broker is
correct **regardless** — one shared token read by both apps is strictly better than N
independent mints whether or not the vendor enforces single-active-token.

## 2. Decision

Make the daily-token backend **pluggable** on the existing `SdkRefresher` seam, with **local
disk as the untouched default** and the shared read-only broker strictly opt-in via
`BRUTEX_TOKEN_BROKER_READONLY=true`:

- **`TokenStore` Protocol** (`brutex/adapters/groww/token_store.py`) — the two-method backend
  contract the disk functions already satisfy (`DiskTokenStore`), plus `resolve_token_store()`
  (flag-selected, fail-safe to local, memoised per env-config so all three refreshers of one
  pull share one client — no split-brain).
- **Read-only Parameter-Store backend** (`token_store_aws.py`) — `ParameterStoreTokenStore`
  reads the shared access-token SecureString via `GetParameter WithDecryption=True`. It is
  **read-only**: `store` fails LOUD with `TokenBrokerReadOnlyError` (a mint attempt on a
  read-only host is a clear, terminal error — never a silent local write that would fork the
  shared token).
- **Cred chain** (`config/credentials.py`) — one additive insertion makes the resolver
  `env → AWS → file`; the AWS tier (`config/credentials_aws.py`) reads the two SecureString
  creds and returns `None` when the broker is not enabled ⇒ byte-identical to today.
- **Region + paths** are configurable constants: region defaults to `ap-south-1`
  (`BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → default); the three parameter paths default to
  `/tickvault/prod/groww/{access-token,api-key,totp-secret}` (each env-overridable).
- **boto3** is a NEW OPTIONAL `token-broker` extra; a local run imports zero AWS code. The
  real boto3 SSM client build is `# pragma: no cover`; a pure-Python `FakeAwsClient` drives the
  broker to 100% coverage with no install and no network.

The Lambda minter + its Terraform are **out of scope** for this PR (a separate PR owns
`deploy/aws_token_minter/`). This PR delivers only BruteX's read-side broker. The
language-agnostic interop spec is `docs/TOKEN_BROKER_CONTRACT.md`; the operator setup is
`docs/runbooks/aws-token-broker.md`.

## 3. Why Parameter Store, single-writer, no DynamoDB / CAS

Because there is **exactly one writer** (the Lambda), there is NO compare-and-swap / write-
contention problem — a plain SecureString parameter is sufficient, and a DynamoDB item +
conditional writes would be unnecessary machinery. BruteX never writes anything. This is the
operator's explicit final design and supersedes an earlier DynamoDB-based draft of this
decision.

A read is a plain `GetParameter WithDecryption=True`, which KMS-decrypts the SecureString
server-side (AWS SSM docs, *"Working with SecureString parameters"* — <https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-securestring.html>;
`GetParameter` API reference — <https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetParameter.html>).

## 4. Consequences

- **Backward-compatible.** `BRUTEX_TOKEN_BROKER_READONLY` unset ⇒ the disk cache + local mint,
  byte-identical to #1041; a local-only run imports no boto3 (regression-pinned). Local dev +
  CI need NO AWS.
- **Fail-safe.** A bad/blank flag / missing boto3 / unbuildable SSM client degrades to local at
  build time (a loud log), never a mid-pull crash. Every SSM read error (unreachable / IAM /
  region / KMS) degrades the token read to a miss, never a raise (the mint spine's contract).
- **Read-only fail-fast.** On a read-only host a forced mint (`store`) fails LOUD rather than
  writing a local token — BruteX never forks the shared token. It picks up the Lambda's
  re-minted token on the next cold-start (process restart).
- **Honest limits.** The cross-app fix is conditional on the UNVERIFIED single-active-token
  behaviour (a live Groww probe would upgrade "designed-to-fix" → "verified-fixes"). Every host
  reads the raw TOTP secret into memory only when a LOCAL mint is needed; in read-only mode the
  token itself is read, not the secret. The read-only store cannot suppress a mint network CALL
  from the injected `token_factory` — only the local WRITE — but the persist fails loud, so no
  silent local token is ever written.
- **Encryption.** The token + creds are SecureStrings (SSE-KMS at rest); the read decrypts
  server-side. BruteX holds no KMS key material and performs no client-side crypto.

## 5. Alternatives considered

- **DynamoDB item + CAS single-flight** — dropped: there is exactly one writer (the Lambda), so
  there is no write contention to arbitrate; a plain SecureString suffices. (This supersedes the
  earlier DynamoDB draft of this same decision.)
- **BruteX mints into the shared store** — dropped: multiple writers reintroduce the mint war;
  the operator's design is a single dedicated Lambda minter, everyone else read-only.
- **Secrets Manager for creds/token** — Parameter Store SecureString is cheaper + already in
  place for the creds; a drop-in if the operator later wants Secrets Manager rotation lambdas.
- **A distinct env prefix per app** — rejected; the whole point is ONE shared place for both
  TickVault and BruteX.

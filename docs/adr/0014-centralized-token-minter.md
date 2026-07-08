# ADR-0014 — Centralized Groww daily-token minter (single-writer, Parameter Store)

**Decision ID:** `DEC-TOKEN-MINTER-CENTRAL-001` (MIGRATION.md entry #1060)
**Date:** 2026-07-01
**Status:** ACCEPTED — design + IaC delivered as CODE ONLY. **NOT deployed.** This
PR ships the Lambda handler + Terraform IaC + docs; the operator deploys to their
AWS account (this session has NO AWS access — see §7 Honesty envelope). Per Golden
Rule #9 an independent 4-agent hostile review runs on this PR before merge; it is
opened as a DRAFT pending that adversarial verification. SUPERSEDED-IN-PART
(2026-07-06, DEC-BROKER-DOCS-AWSONLY-RECONCILE-001): the §8 "#909 remains as a
per-app fallback until the read-side PR points every app at the centralized token"
bullet is superseded — that read-side PR LANDED (DEC-TOKEN-BROKER-AWS-001 #1064,
then AWS-only by DEC-AWS-ONLY-CREDS-001 #1065); production BruteX is read-only
fail-fast and NEVER mints (DEC-REAUTH-COLDSTART-READONLY-001 #1092); body preserved
as the historical record.
**Author:** operator-directed (option C: centralized minter, adapted to existing AWS infra)
**Builds on:** DEC-GROWW-TOKEN-REAUTH-001 (MIGRATION.md #909 — the in-process
auto-re-mint), DEC-GROWW-REAUTH-DEADLINE-001 (#910 — the re-mint deadline guard),
`brutex/adapters/groww/auth.py` (the TOTP + `GrowwAPI.get_access_token` mint logic
this handler mirrors CONCEPTUALLY, re-implemented self-contained).

**Operator directive (design to THIS — no alternatives invented):**
> Region ap-south-1 (Mumbai). Groww creds ALREADY EXIST in Parameter Store as
> SecureStrings (`/tickvault/prod/groww/api-key`, `/tickvault/prod/groww/totp-secret`,
> KMS-encrypted) — do NOT create them. The shared daily access token lives in
> Parameter Store at `/tickvault/prod/groww/access-token` (SecureString). NO
> DynamoDB — single-writer, so no CAS. ONE Lambda is the SOLE minter, on an
> EventBridge schedule (~06:05 IST) + on-demand invoke, with a small retry/backoff
> for a transient issuance 429. A CloudWatch alarm (SNS → operator) fires if the
> Lambda errors or has not minted within the expected window.

---

## 1. Context

BruteX (backtest) and TickVault (live) both authenticate to the Groww Trade API
with a daily access token minted from an API key + a TOTP code
(`GrowwAPI.get_access_token(api_key, totp)`; `brutex/adapters/groww/auth.py`). The
token **resets at 06:00 IST** (verified from production logs 2026-06-23, the
project rules §6 "Token lifetime"). Today each application mints its OWN token on an auth-expiry
mid-run (the in-process auto-re-mint, DEC-GROWW-TOKEN-REAUTH-001 #909). When
BruteX and TickVault run concurrently against the same Groww account, BOTH try to
mint around 06:00 IST — a **"minting storm"** that has made the reset flaky
(competing mints, one invalidating the other's fresh token).

The operator has chosen **option C: one centralized minter** as the permanent fix:
a single AWS Lambda is the SOLE writer of the token, and every application reads it
READ-ONLY.

## 2. Decision

**A single AWS Lambda is the SOLE minter of the shared Groww daily access token,
stored in AWS Systems Manager (SSM) Parameter Store as a SecureString.**

1. On an **EventBridge schedule** (default `cron(35 0 * * ? *)` = 00:35 UTC =
   **06:05 IST**, shortly after the 06:00 IST reset) AND on an **on-demand manual
   invoke**, the Lambda:
   1. reads `/tickvault/prod/groww/api-key` + `/tickvault/prod/groww/totp-secret`
      from Parameter Store via `GetParameter(WithDecryption=True)` (SSM KMS-decrypts
      the SecureStrings) [1];
   2. computes the current TOTP code from the secret (`pyotp`);
   3. mints a fresh token with a pure-stdlib HTTPS POST to the Groww token
      endpoint (`handler.py::get_groww_access_token`, reimplementing the
      request + token-extraction contract of `growwapi` 1.5.0
      `GrowwAPI.get_access_token`, `client.py:1540-1580`, with simpler
      structural error handling — NO `growwapi` dependency, NO Docker; see
      §"Lambda runtime dep is BUNDLED" + DEC-MINTER-PURE-PYTHON-001);
   4. writes the token to `/tickvault/prod/groww/access-token` via
      `PutParameter(Overwrite=True, Type='SecureString', KeyId=<kms key>)` [2].
2. A small **in-handler retry/backoff** absorbs a transient Groww issuance 429
   (exponential `base * 2**attempt`, default 3 retries); a non-throttle failure is
   terminal and re-raised (the Lambda invocation is marked errored).
3. Every mint emits a `TokenMintSuccess` CloudWatch metric (`1` ok / `0` fail).
4. A **CloudWatch alarm** (SNS → the operator's alert endpoint) fires if the metric's
   `FILL(m1, 0)` sum does not reach ≥ 1 within the expected window — a daily-job
   missing-data treatment (`FILL(0)` + `treat_missing_data = "notBreaching"`) that
   catches a wholly-dead Lambda WITHOUT flapping on a healthy day [3] (see
   §"Stale-token alarm — daily-job missing-data treatment"), AND a second alarm on
   the built-in `AWS/Lambda Errors` metric catches a hard invocation failure
   immediately.
5. **TickVault and BruteX read the token READ-ONLY** (GetParameter + KMS Decrypt on
   the token param ONLY). Neither ever mints — the storm is eliminated by
   construction. (The BruteX read-side wiring is a SEPARATE PR; this PR delivers the
   minter + IaC only.)

## 3. Why no DynamoDB (no compare-and-swap) — serialized single writer

A distributed lock / CAS (the classic DynamoDB conditional-write pattern) exists to
coordinate **multiple concurrent writers**. Here the design is a **single writer
that is SERIALIZED** — the Lambda is configured `reserved_concurrent_executions = 1`
(`aws_lambda_function.minter`), so at most one invocation ever runs at a time.
Parameter Store `PutParameter(Overwrite=True)` is a last-writer-wins overwrite, and
over a single serialized writer the last write is always the NEWEST token:

- EventBridge does **not** overlap a still-running invocation of the same scheduled
  rule with another scheduled invocation of that rule [4], so two scheduled mints
  never run at once.
- A manual on-demand invoke COULD otherwise overlap a scheduled mint. Two
  `get_access_token` calls return **distinct** tokens (not "the same value"), so an
  unserialized race — mint(T1) → mint(T2) → put(T2) → put(T1) — could persist the
  OLDER token T1. **`reserved_concurrent_executions = 1` prevents that overlap
  entirely:** the manual invoke queues behind the scheduled one (or is throttled),
  the two mints run strictly in order, and the last `PutParameter` therefore writes
  the newest token.
- Readers never write, so they never contend.

**Groww's session model is UNVERIFIED (Golden Rule #1):** whether a fresh
`get_access_token` INVALIDATES the previously-minted token's session is not
established by any citable Groww source, and this repo's own single-flight in-process
re-mint + token cache (DEC-GROWW-TOKEN-REAUTH-001 #909, DEC-GROWW-TOKEN-DISK-CACHE-001)
treat a re-mint as producing a NEW token that supersedes the old — i.e. the codebase
does not assume "the loser's token is also valid". The earlier draft of this ADR
claimed both mints "write the same correct value" and "the loser's token is also
valid"; both are DROPPED as unsourced. With `reserved_concurrent_executions = 1` the
question is moot for this stack: there is never a concurrent second mint whose token
could be clobbered, so we do not depend on the Groww session-invalidation behavior
either way.

Therefore DynamoDB adds cost + a moving part with **zero** correctness benefit here
(the serialized single writer already gives deterministic ordering). It is
deliberately omitted (Golden Rule #10 — no gratuitous complexity).

## 4. Failure modes + handling

| Failure | Detection | Handling |
|---------|-----------|----------|
| Transient Groww issuance 429 (throttle) | `_is_rate_limit(exc)` (code 429 / "rate limit" / "too many request") | In-handler exponential backoff, up to `max_retries`; then terminal |
| Wrong TOTP secret / expired API key / IP not whitelisted | mint raises a non-throttle error | Terminal: `TokenMintError` (sanitized reason), invocation errored, `Errors` alarm fires |
| Groww returns a clean but EMPTY token | post-mint `token.strip()` is blank | Terminal `TokenMintError` — a blank token is NEVER written |
| Credential param missing / blank in SSM | `_read_secure_string` → `MinterConfigError` | Terminal, failure metric emitted, `Errors` alarm fires |
| Misconfigured env (missing param path / KMS id) | `resolve_config` → `MinterConfigError` | Terminal (before any AWS call); the missing-datum stale-token alarm still catches a wholly-dead Lambda |
| CloudWatch metric emit fails | `_emit_metric` try/except | Swallowed + logged — never masks the real mint outcome; the missing-datum alarm still fires on a total metric outage |
| Lambda never runs / times out / crashes | no `TokenMintSuccess=1` in the window | Stale-token alarm — metric math `FILL(m1, 0)` over a 24h SUM (`treat_missing_data="notBreaching"` for the pre-first-mint bootstrap only) — pages the operator BEFORE the stale token breaks a pull (see §"Stale-token alarm — daily-job missing-data treatment") |
| Secret leakage in logs | `_sanitize` scrubs api-key / TOTP secret / minted code to `***`; on a terminal issuance error the `TokenMintError` is raised `from None` so the raw vendor exception is NOT chained | Only the sanitized reason is logged / carried in `str(exc)`; no secret reaches CloudWatch Logs (§"Secret hygiene") |

### Secret hygiene — the `__cause__` chain is broken (`from None`)

The terminal issuance raise is `raise TokenMintError(f"...{last_reason}") from None`,
**not** `from exc`. The raw vendor SDK exception `exc` may ECHO the api-key / TOTP in
its message; if it stayed chained as `__cause__`, an uncaught propagation out of
`lambda_handler` would make the AWS Lambda runtime log the FULL chained traceback
(raw `exc` included) to CloudWatch — leaking the secret. `from None` severs that
chain. The SANITIZED `last_reason` is logged (`groww.token.issuance_failed`) AND
carried in `str(TokenMintError)`, so no diagnostic signal is lost. The TOTP
computation is also inside the guarded `try`, so a malformed-secret error from
`pyotp` is sanitized too, never raised raw. **No secret ever reaches CloudWatch
Logs** — this is now enforced by construction, not merely asserted.

### Stale-token alarm — daily-job missing-data treatment (no healthy-day flapping)

The `TokenMintSuccess` metric is a **daily-job** metric: one real datapoint/day
(~00:35 UTC) and ~23.5h/day with no datapoint. A naive `treat_missing_data =
"breaching"` over a continuous sliding-window evaluation is the classic daily-job
anti-pattern — every period with a missing datum is padded "breaching", so the alarm
FLAPS ALARM/OK every day on a HEALTHY pipeline. The `stale_token` alarm instead uses
metric math **`FILL(m1, 0)`** over a 24h `Sum` window (`m1` = the raw metric,
`return_data=false`; `e1 = FILL(m1, 0)`, `return_data=true`): missing periods are
filled with an explicit 0, so the alarm evaluates a DENSE series. A genuine mint
(1 in the window) → filled sum ≥ 1 → OK; NO success at all (a dead/never-run Lambda
that emits nothing, all periods fill to 0; OR every mint FAILING, each emitting 0) →
filled sum 0 < 1 → ALARM. `treat_missing_data = "notBreaching"` now governs only the
genuinely-empty bootstrap window before the first-ever mint. The failure direction is
PRESERVED (real 0.0 → fire; silence → fire) and the healthy-day flapping is gone
(AWS documents `FILL(<metric>, <value>)` as the recommended treatment for a
sparse/daily metric — §"AWS API references" [3]). The separate `lambda_errors` alarm
on `AWS/Lambda Errors` is unchanged (a hard invocation failure pages immediately).

### Lambda runtime dep is BUNDLED (real import at runtime) — pure Python, NO Docker

`boto3` is on the AWS Lambda Python runtime, but `pyotp` is NOT — the handler's lazy
`import pyotp` (TOTP computation) would ImportError on the first real mint if it were
missing. The build step `build_lambda_package.sh` (run by the
`null_resource.build_lambda_package` before `archive_file` zips, and by `deploy.sh`
before `terraform apply`) `pip install`s the one pinned dep (`var.pyotp_version`)
INTO the deploy bundle staging dir alongside the handler, so the deployed Lambda
actually imports at runtime. This is REAL packaging, not a documented manual step.

**The token mint is a pure-stdlib HTTPS POST — `growwapi` is NOT bundled
(DEC-MINTER-PURE-PYTHON-001, docs/MIGRATION.md #1069).** The handler's
`get_groww_access_token` reimplements the REQUEST + token-extraction CONTRACT of
`growwapi` 1.5.0 `GrowwAPI.get_access_token` (`client.py:1540-1580`): the Groww TOTP
flow is exactly ONE unsigned HTTPS POST to `https://api.groww.in/v1/token/api/access`
with seven plain headers and a `{"key_type":"totp","totp":...}` body — no request
signing, no crypto, no pynacl — so `urllib` alone replicates it exactly. The ERROR
handling is deliberately SIMPLER than the SDK's (rather than a line-for-line copy of
its exception tree): a non-2xx raises a single `GrowwMintHTTPError` that carries the
HTTP status `code` STRUCTURALLY (so `_is_rate_limit`'s `code == 429` retries a
phrase-free 429 body), and a blank TOTP raises `ValueError` before any network call
(matching the SDK's `_build_request_data` guard, `client.py:1526-1527`). Dropping the
`growwapi` import removes its heavy native-extension transitive tree (`pynacl`,
`pydantic-core`, `pandas`, `aiohttp`, `protobuf`, and the sdist-only `clint`) from the
bundle. The Groww HTTP contract is cited inline in the handler to the exact `client.py`
line, so no external claim is invented (Golden Rule #1).

**Because the ONLY bundled dep is now `pyotp` — a pure-Python UNIVERSAL wheel
(`py3-none-any`, zero runtime deps) — the bundle carries ZERO platform-specific
wheels**, so `pip install --target` produces only pure `.py` (no `.so`/`.dylib`/
`.pyd`) and the artifact runs unchanged on the Lambda's x86_64 Linux / cpython 3.12
runtime regardless of the build host. The operator builds on macOS/ARM64 (project
rules §8: Apple M4 Pro) and the SAME bundle deploys — with **no Docker**, no
`--platform`/`--only-binary`/`--abi` cross-compile flags, and no two-path branching.
This SUPERSEDES the two-path Docker/cross-pip build of DEC-MINTER-BUILD-ROBUST-001
(the prior revision of PR #1027), which existed only to work around `growwapi`'s
native tree + `clint`'s missing wheel — both now gone with `growwapi` itself.

**`pyotp` IS PINNED.** The default is `pyotp==2.10.0` (latest within BruteX's
`pyproject.toml` `pyotp>=2.9,<3.0`) — the same range BruteX itself pins, so the
Lambda and the app share one dependency truth. `deploy/aws_token_minter/requirements.txt`
declares it once; the `build_lambda_package.sh` `PYOTP_VERSION` default and the
Terraform `var.pyotp_version` default carry the same pin.

A `tests/deploy` content-assertion pins the single-dep pure-Python build (pyotp
pinned; no Docker/growwapi/boto3 in the build; no cross-compile flags; no two-path
branching) so a regression cannot silently reintroduce them.

**No Docker step remains.** On a fresh macOS machine with `pip` + PyPI access,
`./deploy.sh` (or `terraform apply`) builds the bundle in one deterministic
`pip install` and succeeds — the operator's original `ResolutionImpossible` cannot
recur because there is no cross-platform native resolve to fail.

**HONEST LIMITS (Golden Rule #10).** The endpoint, headers, request body, and token
extraction are PINNED to `growwapi` 1.5.0's behavior (cited to `client.py:1540-1580`).
If Groww changes its auth contract, `handler.py::get_groww_access_token` must be
updated — the SAME version-coupling the SDK itself has (the previous design was
coupled to the pinned `growwapi==1.5.0` wheel; this design is coupled to the same
1.5.0 HTTP contract, now visible and testable in-repo instead of vendored opaquely).

## 5. Least-privilege IAM matrix

Three roles, each scoped to specific resource ARNs (never `*` except where the AWS
API forbids resource-level scoping — noted). ARNs are derived from the `param_prefix`
+ the resolved account/region, so a re-point needs no policy edit.

| Role | ssm:GetParameter | ssm:PutParameter | kms:Decrypt | kms:Encrypt | CloudWatch |
|------|------------------|------------------|-------------|-------------|------------|
| **(a) minter** (Lambda) | `api-key` + `totp-secret` params | `access-token` param | the one KMS key | the one KMS key | `PutMetricData` (namespace-scoped condition) + own log group |
| **(b) reader-tickvault** | `access-token` param ONLY | — | the one KMS key | — | — |
| **(c) reader-brutex** | `access-token` param ONLY | — | the one KMS key | — | — |

- `PutMetricData` cannot be resource-scoped in IAM (resource is `*`); it is
  instead constrained by a `cloudwatch:namespace` `StringEquals` condition [5].
- `kms:GenerateDataKey` is included in the minter's KMS statement because SSM's
  `PutParameter` for a SecureString uses envelope encryption (a data key generated
  under the CMK) [6].
- The two credential params + the KMS key are referenced via `data` sources and
  are **never created or managed** by this stack.
- The reader roles' trust policy is scoped to the account root as a base; the
  operator narrows it to the exact TickVault/BruteX compute principal ARNs when
  wiring the roles to their workloads (documented in the runbook).

## 6. Alternatives considered

- **Secrets Manager instead of Parameter Store** — Secrets Manager has built-in
  rotation Lambdas, but the operator's creds ALREADY live in Parameter Store and
  the token is a low-churn single value; SecureString + a scheduled Lambda is
  simpler and cheaper. Rejected to match existing infra (operator directive).
- **DynamoDB single-token table + CAS** — rejected, see §3.
- **In-process per-app minting (status quo)** — the storm this ADR fixes. Kept as
  the fallback only if the centralized token is unreadable (a read-side concern for
  the separate consumer PR, not decided here).

## 7. Honesty envelope (Golden Rule #10 + Golden Rule #1)

- **NOT deployed / NOT proven in AWS.** This session has NO AWS access. We deliver
  CODE + IaC; the operator runs `terraform apply`. No claim is made that the Lambda
  runs, mints, or that any alarm has fired in a real account.
- **Terraform verified only as far as offline allows.** `terraform fmt -check` is
  clean and `terraform init`'s config-parse + variable/expression validation passes
  (it caught + we fixed a heredoc-interpolation bug). Full `terraform validate`
  needs the AWS provider schema, and the provider registry is blocked by the
  sandbox egress policy (403) — so schema-level validate is **UNVERIFIED here** and
  is an operator step (`terraform init && terraform validate`).
- **AWS behavioural claims are CITED, not invented** (§"AWS API references" below).
- **The read-side is OUT OF SCOPE** of this PR (a separate PR wires BruteX to read
  the centralized token; TickVault is a separate repo).
- **A 429 is now caught STRUCTURALLY** — an HTTP-429 mint failure raises
  `GrowwMintHTTPError` carrying `.code == 429`, so `_is_rate_limit` classifies it
  retryable REGARDLESS of the vendor body's wording (the structural check does not
  depend on the body encoding). The phrase-based `_RATE_LIMIT_HINTS` fallback
  (mirroring `brutex/adapters/groww/errors.py`) remains a best-effort secondary for a
  non-HTTPError throttle; the exact textual encoding of a Groww 429 body stays
  UNVERIFIED but the retry no longer relies on it.
- **Groww's single-active-token session model is UNVERIFIED** (no citable source;
  see §3). The design does not depend on it — `reserved_concurrent_executions = 1`
  serializes the single writer so no concurrent mint can clobber a newer token.
- **The Lambda dep is bundled (not a manual step) — pure Python, NO Docker
  (DEC-MINTER-PURE-PYTHON-001).** The token mint is a pure-stdlib HTTPS POST
  (`handler.py::get_groww_access_token`, reimplementing the request +
  token-extraction contract of `growwapi` 1.5.0 `GrowwAPI.get_access_token`,
  `client.py:1540-1580`, with simpler structural error handling), so
  `growwapi` — and its heavy native-extension transitive tree (`pynacl`,
  `pydantic-core`, `pandas`, `aiohttp`, the sdist-only `clint`) — is NOT bundled.
  The ONLY bundled dep is `pyotp==2.10.0`, a pure-Python UNIVERSAL wheel
  (`py3-none-any`, zero runtime deps), so the bundle has ZERO platform-specific
  wheels and `build_lambda_package.sh` builds identically on macOS/ARM64 and Linux
  with no Docker, no cross-compile flags, and no two-path branching. This
  SUPERSEDES the two-path Docker/cross-pip build of DEC-MINTER-BUILD-ROBUST-001
  (#1063), which existed only to work around `growwapi`'s native tree; with
  `growwapi` gone the operator's `ResolutionImpossible` cannot recur (no
  cross-platform native resolve to fail). A content-assertion test pins the
  single-dep pure-Python build (pyotp pinned; no Docker/growwapi/boto3; no
  cross-compile flags). HONEST (Golden Rule #10): VERIFIED IN THE SANDBOX —
  `pip install --target --no-compile pyotp==2.10.0` succeeds and the bundle contains
  ZERO `.so`/`.dylib`/`.pyd` files (`Tag: py3-none-any`, `Root-Is-Purelib: true`);
  `build_lambda_package.sh` runs end-to-end (exit 0) producing a clean handler+pyotp
  bundle with no platform binaries. The pure request/parse/error branches of
  `get_groww_access_token` are covered by an injected fake opener (no network). NOT
  PROVEN IN THE SANDBOX (no AWS/live-vendor access): the real mint against the live
  Groww endpoint and end-to-end import in the deployed x86_64 Lambda remain operator
  steps. The Groww HTTP contract is PINNED to `growwapi` 1.5.0 (cited to
  `client.py:1540-1580`); if Groww changes its auth contract, `handler.py` must be
  updated — the same version-coupling the SDK itself has.

## Groww token-mint HTTP contract (Golden Rule #1 — no hallucination)

The handler's pure-stdlib `get_groww_access_token` replicates the Groww TOTP
token flow exactly as `growwapi` 1.5.0 implements it — verified from the SDK
source, not invented:

- URL — `https://api.groww.in/v1/token/api/access` (`client.py:1557`)
- Method — `POST`, JSON body, `timeout=15` (`client.py:1560`)
- Headers — `x-request-id` = `str(uuid.uuid4())`, `Authorization` =
  `"Bearer " + api_key`, `Content-Type: application/json`, `x-client-id: growwapi`,
  `x-client-platform: growwapi-python-client`, `x-client-platform-version: 1.5.0`,
  `x-api-version: 1.0` (`client.py:1370-1378`, `_build_headers`)
- Body — `{"key_type": "totp", "totp": totp.strip()}` (`client.py:1525-1529`,
  `_build_request_data`)
- Token extraction — `response.json()["token"]` (`client.py:1580`)
- Errors — non-2xx raises with the vendor message preserved (`client.py:1561-1579`)

## AWS API references (Golden Rule #1 — no hallucination)

1. SSM `GetParameter` `WithDecryption` (KMS-decrypts a SecureString):
   https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetParameter.html
2. SSM `PutParameter` `Overwrite` / `Type=SecureString` / `KeyId`:
   https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_PutParameter.html
3. CloudWatch alarm `TreatMissingData` + metric-math `FILL(<metric>, <value>)` as the
   recommended treatment for a sparse/daily metric (so missing periods read as an
   explicit value instead of the alarm's missing-data padding — the daily-job
   no-flapping fix):
   https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html#alarms-and-missing-data
4. EventBridge scheduled rules do not overlap a still-running invocation of the same
   rule (retry/overlap semantics):
   https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-run-lambda-schedule.html
5. IAM `cloudwatch:namespace` condition key for `PutMetricData` (which has no
   resource-level permissions):
   https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatch.html
6. SSM SecureString envelope encryption with KMS (`GenerateDataKey`):
   https://docs.aws.amazon.com/kms/latest/developerguide/services-parameter-store.html

## 8. Consequences

- The multi-app minting storm is eliminated by construction (single writer).
- The 06:00 IST reset self-heals unattended via the scheduled mint; the operator is
  paged BEFORE a stale token breaks a pull (both alarms).
- A new operational surface (one Lambda + schedule + 2 alarms + 3 roles) the
  operator owns; the runbook (`docs/runbooks/aws-token-minter.md`) documents the
  one-command deploy (`cd deploy/aws_token_minter && ./deploy.sh`, which builds the
  bundle, `terraform init`s, and shows a plan requiring a `yes` — never
  auto-approved) + the on-demand mint + the read-side wiring.
- The in-process auto-re-mint (#909) remains as a per-app fallback until the
  read-side PR points every app at the centralized token.

# Operator deploy guide — AWS centralized Groww token system

> **Decision:** `DEC-AWS-DEPLOY-GUIDE-001` (MIGRATION.md #1067).
> **Audience:** the operator standing up the whole centralized-token system for the
> first time. Plain English, copy-paste commands, in order.
> **Companion docs (do not duplicate — read for detail):**
> `docs/runbooks/aws-token-minter.md` (the minter's exact paths/schedule/rollback,
> DEC-TOKEN-MINTER-CENTRAL-001 / ADR-0014) and `docs/runbooks/aws-token-broker.md`
> (BruteX's read-only Parameter-Store reader — LANDED as PR #1015 /
> DEC-TOKEN-BROKER-AWS-001, MIGRATION.md #1064, then made the DEFAULT AND ONLY
> credential/token source by DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065).

## What this sets up (the 30-second version)

Both BruteX (backtest) and TickVault (live) need a **daily Groww access token**.
That token **resets at 06:00 IST** every day. If both apps mint their own token
around 06:00, they collide (a "minting storm") and the reset goes flaky.

This system fixes it with **one writer**:

```
                 EventBridge  ~06:05 IST  ─┐
                                           ▼
   Parameter Store                   ┌──────────────┐        Parameter Store
   /tickvault/prod/groww/api-key ──► │  ONE Lambda  │ ──────► /tickvault/prod/groww/
   /tickvault/prod/groww/totp-secret │  (the SOLE   │         access-token
                                     │   minter)    │            │      │
                                     └──────┬───────┘            │      │
                                            │ CloudWatch alarm   │      │
                                            ▼ (SNS → your email) ▼      ▼
                                      you get paged if      BruteX   TickVault
                                      a mint ever fails     (read-only, never mint)
```

**One AWS Lambda** reads the two Groww secrets, computes the TOTP, mints the token,
and writes it to `access-token` in Parameter Store — on a schedule (~06:05 IST) and
on demand. **BruteX and TickVault only ever READ** that token. No app mints, so the
storm is gone by construction.

> **HONESTY — code + infrastructure only, YOU run the deploy.** This repository ships
> the Lambda handler, the Terraform (infrastructure-as-code), the `deploy.sh` script,
> and this guide. **Nothing is deployed to any AWS account by CI or by this repo** —
> there is no auto-deploy. You run `./deploy.sh` from your own admin machine against
> your own AWS account. Every AWS behavioral claim below is cited to an official AWS
> docs URL (see [AWS references](#aws-references)); nothing about AWS is invented.

---

## 1. Prerequisites (one-time)

You need four things before you deploy. Check each.

### 1a. AWS admin credentials, region ap-south-1

You need an AWS identity that can create a Lambda, IAM roles, an EventBridge rule,
CloudWatch alarms, an SNS topic, and one SSM parameter. Configure the CLI:

```bash
aws configure
# AWS Access Key ID     [None]: <your admin key>
# AWS Secret Access Key [None]: <your admin secret>
# Default region name   [None]: ap-south-1
# Default output format [None]: json
```

Confirm it works and points at the right account + region:

```bash
aws sts get-caller-identity        # prints your account + user/role ARN
aws configure get region           # must print: ap-south-1
```

> The whole system lives in **`ap-south-1` (Mumbai)** because that is where your
> Groww SecureStrings already live. All the defaults below assume it.

### 1b. Terraform + AWS CLI installed

```bash
brew install terraform awscli      # macOS (Homebrew)
```

Confirm versions (Terraform must be **≥ 1.5**):

```bash
terraform version                  # e.g. Terraform v1.9.x
aws --version                      # e.g. aws-cli/2.x
```

> Not on macOS? Install Terraform from
> <https://developer.hashicorp.com/terraform/install> and the AWS CLI from
> <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>.

### 1c. The two Groww secrets already seeded in Parameter Store

The minter **reads** (never creates) your two Groww credentials. They must already
exist as **SecureStrings** at these exact paths:

| Parameter | What it is |
|-----------|------------|
| `/tickvault/prod/groww/api-key` | your Groww API key (SecureString) |
| `/tickvault/prod/groww/totp-secret` | your Groww TOTP secret (SecureString) |

Verify they are present (this prints the parameter metadata, **not** the secret):

```bash
aws ssm describe-parameters --region ap-south-1 \
  --parameter-filters "Key=Name,Values=/tickvault/prod/groww/api-key,/tickvault/prod/groww/totp-secret" \
  --query "Parameters[].{Name:Name,Type:Type,KeyId:KeyId}" --output table
```

You should see both names, `Type = SecureString`. **If you have not seeded them
yet**, do so once (replace the values with your real ones — quote them so the shell
does not eat any special characters):

```bash
aws ssm put-parameter --region ap-south-1 --type SecureString \
  --name /tickvault/prod/groww/api-key   --value 'YOUR_GROWW_API_KEY'

aws ssm put-parameter --region ap-south-1 --type SecureString \
  --name /tickvault/prod/groww/totp-secret --value 'YOUR_GROWW_TOTP_SECRET'
```

> **Which KMS key encrypts these two?** It does not matter for the minter by default.
> Whatever key you used — the AWS-managed default `alias/aws/ssm` (what you get by
> omitting `--key-id`, and the likely key if you seeded these before the custom key
> existed) **or** the custom `alias/tickvault-groww` — the minter can read them,
> because the stack grants the Lambda `kms:Decrypt` on the default SSM key **tightly
> scoped to decryption *through SSM only*** (`kms:ViaService = ssm.ap-south-1.amazonaws.com`).
> This is on by default (`allow_ssm_default_key_decrypt = true`), so **you do NOT have
> to re-put the creds** under the custom key. (Set `-var 'allow_ssm_default_key_decrypt=false'`
> only if you have already put BOTH creds under `alias/tickvault-groww` and want the
> minter's KMS reach limited to that one key — then re-put them with `--key-id
> alias/tickvault-groww --overwrite` first, or the mint fails with `KMSAccessDeniedException`.)

> The `access-token` parameter is **not** something you create — the Lambda creates
> and writes it (encrypted under the custom `alias/tickvault-groww` key). Only these
> two credentials are your responsibility.

### 1d. The KMS key exists — create it first if it does not

The two SecureStrings are encrypted by `alias/tickvault-groww`. This stack
**references** that key via a Terraform `data` source and **never creates or
manages it** (ADR-0014 §5 — deliberately, so `terraform destroy` can never delete
the key guarding your real Groww creds). So the key must exist **before** you
apply, or `terraform apply` dies mid-plan with:

```
Error: reading KMS Key (alias/tickvault-groww): couldn't find resource
```

Confirm it resolves:

```bash
aws kms describe-key --region ap-south-1 --key-id alias/tickvault-groww \
  --query "KeyMetadata.{KeyId:KeyId,State:KeyState}" --output table
```

You want `State = Enabled`. The key is needed **regardless of where your two creds
live**, because the stack encrypts the `access-token` it creates under this custom
key. **If it does not exist yet**, create the customer-managed key and the alias this
stack expects (one time):

```bash
# create the key — note the KeyId it prints
aws kms create-key --region ap-south-1 \
  --description 'Groww token-minter SecureString key (tickvault)' \
  --query KeyMetadata.KeyId --output text

# alias it (use the KeyId from the line above)
aws kms create-alias --region ap-south-1 \
  --alias-name alias/tickvault-groww --target-key-id <KeyId-from-above>
```

> **You do NOT need to re-put your creds under this key.** By default the minter is
> granted `kms:Decrypt` on the AWS-managed default SSM key (`alias/aws/ssm`) too —
> tightly scoped to decryption *through SSM only* (`kms:ViaService`), see §1c — so
> creds seeded under the default key are read fine with no re-put. The only case that
> needs a re-put is if you deliberately pass `-var 'allow_ssm_default_key_decrypt=false'`
> to limit the minter to the one custom key; then put **both** creds under it (§1c's
> `put-parameter` with `--key-id alias/tickvault-groww --overwrite`, plaintext again)
> or the mint fails with `KMSAccessDeniedException`.

`./deploy.sh` probes this key for you before it builds or applies: if it is missing
the script prints these create commands and stops, so you never hit the raw Terraform
error. (If you use a different key alias, pass it with `-var 'kms_key_alias=alias/your-key'`
— the pre-flight honors the override.)

---

## 2. The one-time deploy

One command builds the Lambda bundle and provisions everything. It takes ~2 minutes.

```bash
cd deploy/aws_token_minter
./deploy.sh -var 'alert_email=you@example.com'
#   or:  ALERT_EMAIL=you@example.com ./deploy.sh
```

**What `./deploy.sh` does, in order** (it never auto-approves — see below):

0. **Pre-flight** — checks Terraform is on `PATH` and (if the `aws` CLI is present)
   that the KMS key `alias/tickvault-groww` resolves in the target region. A
   missing key stops the script **before** the build with the exact `aws kms
   create-key` / `create-alias` + cred re-put commands (§1d), so you never hit the
   opaque mid-plan `couldn't find resource` Terraform error. It honors your
   `-var 'kms_key_alias=…'` / `-var 'region=…'` overrides.
1. **Builds the Lambda bundle** (`build_lambda_package.sh`) — pip-installs `pyotp`
   into the deploy zip. That is the ONLY bundled dependency; it is a pure-Python
   universal wheel (zero platform-specific files), so the bundle imports cleanly on
   the Lambda's x86_64 Linux / Python 3.12 runtime **no matter what you build on**
   — including an Apple-silicon Mac (project rules §8) — with **no Docker**. The
   Groww token is minted by a pure-stdlib HTTPS POST in the handler (no `growwapi`),
   which is what removed the old cross-compile/Docker requirement
   (DEC-MINTER-PURE-PYTHON-001). This step needs `pip` + PyPI network access on your
   machine.
2. **Bootstraps the remote state backend** — idempotently creates (if missing)
   an **S3 bucket** `tickvault-tfstate-<ACCOUNT_ID>` (versioning + AES256 SSE +
   public-access-block) and a **DynamoDB lock table** `tickvault-tfstate-lock`
   (`LockID`, PAY_PER_REQUEST) via the AWS CLI. Terraform's state lives in AWS,
   not on your disk, so a fresh clone on any machine reads the SAME state. Re-runs
   are no-ops (it guards with `head-bucket` / `describe-table`). See §2b.
3. **`terraform init`** — downloads the AWS/archive/null providers and wires the
   S3 + DynamoDB backend (via `-backend-config`).
4. **Adopts pre-existing resources into the state** (`DEC-DEPLOY-AUTO-FIRST-MINT-001`)
   — if any of the stack's 7 name-fixed resources (the `access-token` SSM param, the
   3 IAM roles, the Lambda log group, the Lambda function, the EventBridge invoke
   permission) already **exists in AWS but is missing from the remote state** — the
   classic local→remote state-migration gap (§2b), or an out-of-band creation — it is
   `terraform import`-ed automatically, so the apply never dies with
   `ParameterAlreadyExists` / `EntityAlreadyExists` (409). Idempotent: already-in-state
   and not-yet-created resources are honest one-line no-ops; a **failed import
   hard-stops the deploy** with the verbatim Terraform error (it never continues into
   a doomed apply).
5. **`terraform apply`** — Terraform **shows you the full plan and stops, waiting for
   you to type `yes`.** Nothing is created until you confirm. This script **never**
   passes `-auto-approve` — a human confirms every apply.
6. **First token mint** (`DEC-DEPLOY-AUTO-FIRST-MINT-001`) — invokes the
   freshly-applied minter Lambda **once** and verifies the response reports
   `ok: true`, printing an honest `FIRST MINT: PASS` / `FAIL` line. The shared
   `access-token` param is therefore **live the moment the deploy finishes** — you
   never run a manual `aws lambda invoke`, and there is no empty-token window until
   the next 06:05 IST cron. A mint failure prints the exact error text and **exits
   non-zero** so you SEE it. Re-running `./deploy.sh` re-mints once per deploy —
   harmless: the Lambda is the sole token writer and the SSM write is
   last-writer-wins (`PutParameter Overwrite=True`).

**What it creates** (all in `ap-south-1`, all least-privilege):

- the **minter Lambda** `groww-token-minter` (concurrency **unreserved by default** —
  `lambda_reserved_concurrency = -1`, so it applies on a fresh account whose total
  Lambda concurrency is the default 10; see the note below to opt into the strict
  single-writer serialization);
- the `access-token` **SSM SecureString** parameter it writes;
- an **EventBridge schedule** `cron(35 0 * * ? *)` = 00:35 UTC = **06:05 IST**;
- a **stale-token CloudWatch alarm** (pages if no successful mint in 24h) + a
  **Lambda-errors alarm** (pages on any hard failure) + the **SNS topic** they publish to;
- **three least-privilege IAM roles** — the minter (read the 2 creds, write the
  token; `kms:Decrypt`/`Encrypt` on the custom key for the token write, plus
  Decrypt-only on the default `alias/aws/ssm` key scoped to *SSM-only* via
  `kms:ViaService` so it can read creds under either key with no re-put — the latter
  toggleable with `-var 'allow_ssm_default_key_decrypt=false'`), plus `reader-tickvault`
  and `reader-brutex` (each can ONLY `GetParameter` the token + `kms:Decrypt` the
  custom key, nothing else).

**After `apply` succeeds, confirm the SNS email subscription.** AWS emails a
confirmation link to `alert_email`; the alarm cannot page you until you click it.

Read the plan before you type `yes` — you should see roughly one Lambda, one SSM
parameter, one EventBridge rule, two alarms, one SNS topic, and three IAM roles being
**added**, and **zero** to change or destroy on a fresh account.

> Want to override a default (different account prefix, KMS alias, schedule)? Pass
> extra `-var` flags straight through — see `docs/runbooks/aws-token-minter.md` §4.

> **Reserved concurrency (default `-1`, unreserved).** A brand-new AWS account has a
> total Lambda concurrency of **10**, and *reserving* any concurrency must leave at
> least 10 unreserved — so a hard-coded reservation of 1 is **rejected** on a default
> account with
> `InvalidParameterValueException: … decreases account's UnreservedConcurrentExecution
> below its minimum value of [10]`. The stack therefore defaults
> `lambda_reserved_concurrency = -1` (unreserved), which applies everywhere. This is
> safe for the minter: it is the **sole** token writer, a **daily single-shot** cron,
> and its SSM write is idempotent last-writer-wins — a concurrent double-mint is both
> unlikely and harmless (both write a valid fresh token). **If your account has
> concurrency headroom** (a raised limit) and you want the strict single-writer
> hardening, opt in with `./deploy.sh -var 'lambda_reserved_concurrency=1'`.

---

## 2b. Fresh clones + repeat deploys (remote state — `DEC-DEPLOY-REMOTE-STATE-001`)

**Why this matters.** Terraform records what it created in a *state file*. If that
state lives on your laptop (`terraform.tfstate`, gitignored), a **fresh clone on
another machine has no state** — so `terraform apply` there thinks nothing exists
and tries to **re-create** the Lambda / SSM param / IAM roles, which errors with
`EntityAlreadyExists` / `ResourceInUse`. The deploy would not be repeatable.

**The fix: state lives in AWS.** `deploy.sh` keeps Terraform state in an **S3
bucket** with a **DynamoDB lock table**, so every clone on every machine reads the
SAME state and `apply` is **idempotent** — run it any number of times, from any
fresh clone, and it converges to a no-op once the stack exists (no "already exists"
errors). `deploy.sh` bootstraps the bucket + table itself (check-then-create) before
`terraform init`, so there is nothing extra for you to run on a fresh account.

**The fresh-clone flow (any machine, any number of times):**

```bash
git clone <this-repo> && cd bruteX     # or: cd bruteX && git pull
cd deploy/aws_token_minter
./deploy.sh -var 'alert_email=you@example.com'
#   deploy.sh:
#     - creates the S3 state bucket + DynamoDB lock table if they are missing
#       (idempotent — a re-run just reuses them);
#     - runs `terraform init` pointed at that remote backend (reads the SHARED
#       state — so it SEES the already-deployed stack);
#     - runs `terraform apply` — on an already-deployed stack the plan is
#       "0 to add, 0 to change, 0 to destroy". Type `yes`; it is a no-op.
```

That is the whole point: **fresh clone → `./deploy.sh` → idempotent**, from any
machine, any number of times.

### One-time migration if you ALREADY deployed with local state

If you deployed *before* this change, your existing state is a local
`terraform.tfstate` file. Move it into S3 **once** (Terraform copies it for you):

```bash
cd deploy/aws_token_minter

# The bucket/table are created by a first ./deploy.sh run (or create them by hand
# with the same names). Then migrate your existing local state into S3:
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
terraform init -migrate-state \
  -backend-config="bucket=tickvault-tfstate-${ACCOUNT_ID}" \
  -backend-config="dynamodb_table=tickvault-tfstate-lock" \
  -backend-config="region=ap-south-1"
#   Terraform prints:
#     "Do you want to copy existing state to the new backend?"
#   Answer  yes  — it copies your local terraform.tfstate up to S3.
```

After that one-time migration, delete/ignore the local `terraform.tfstate`
(already gitignored) and use `./deploy.sh` from then on — every fresh clone reads
the remote state. `deploy.sh` itself always runs a plain `terraform init` (never
`-migrate-state`), so it never silently moves your state; the migration above is a
deliberate, one-time, confirm-`yes` step.

> **Didn't migrate (or can't — the laptop with the old local state is gone)?**
> `deploy.sh` step 4 (`DEC-DEPLOY-AUTO-FIRST-MINT-001`) now **auto-adopts** the 7
> name-fixed resources (`terraform import`s each one that exists in AWS but is
> missing from the remote state), so the fresh-clone
> `ParameterAlreadyExists` / `EntityAlreadyExists` conflicts self-heal and the
> apply converges. The `-migrate-state` path above remains the cleaner option when
> you still HAVE the local state (it carries the FULL state, not just the 7
> adoptable resources); once the state is shared in S3 this gap cannot recur — the
> preflight then just prints seven "already in state (no-op)" lines.

> **Before / after.** *Before:* local state → a fresh clone loses it → `apply`
> tries to re-create existing resources → `EntityAlreadyExists` conflicts. *After:*
> state in S3 (+ DynamoDB lock) → any clone on any machine reads the same state →
> `apply` is idempotent, converging to a no-op, no conflicts.

---

## 3. How BruteX reads it

BruteX reads the token **read-only** from Parameter Store — **unconditionally**
(DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065). One setting on the machine that runs
the pull:

### 3a. Give BruteX a read-only AWS identity

Configure the AWS CLI on the BruteX machine with a **read-only** key — an identity
whose only permission is `GetParameter` on the token + `kms:Decrypt` (attach the
`reader-brutex` role's policy, or use a key assuming that role):

```bash
aws configure                      # region: ap-south-1  (read-only credentials)
```

Get the exact reader role ARN Terraform created (run from `deploy/aws_token_minter`):

```bash
terraform output reader_brutex_role_arn
```

### 3b. Nothing to turn on — just pull

There is **no toggle**: BruteX resolves the shared token from Parameter Store
**unconditionally**. The former `BRUTEX_TOKEN_BROKER_READONLY` opt-in flag and the
"local on-disk cache + local TOTP mint" fallback it guarded were REMOVED — the
"later change" earlier revisions of this guide promised has MERGED
(DEC-AWS-ONLY-CREDS-001, MIGRATION.md #1065; the removal is quoted verbatim in the
`brutex/adapters/groww/token_store.py:8-10` module docstring).

```bash
# region defaults to ap-south-1; override only if your token lives elsewhere:
# export BRUTEX_TOKEN_AWS_REGION=ap-south-1

# ...then run your normal BruteX pull (dashboard or CLI).
```

**What happens on failure (fail-FAST, never a silent local mint):** a missing
boto3 extra / an unbuildable SSM client raises a `GrowwAuthError` naming the
deploy pointer `deploy/aws_token_minter/deploy.sh`
(`token_store.py::resolve_token_store`, :109-117 — "no `BRUTEX_TOKEN_BROKER_READONLY`
gate and no local-mint fallback. This is FAIL-FAST"); a credential-read failure
raises `CredentialsError` (`brutex/config/credentials_aws.py:1-27` — never env vars,
a local file, or the removed webpage); BruteX **never mints** the shared token —
the Lambda owns minting, and the read-only cold start cannot even reach the mint
call (DEC-REAUTH-COLDSTART-READONLY-001, MIGRATION.md #1092). Local dev / CI
without AWS therefore cannot run a live pull — by design (Golden Rule #1: a token
comes from AWS or the pull fails LOUD). (See `docs/runbooks/aws-token-broker.md`
for the reader's full contract.)

> The token param path, the region, and the credential paths are all
> env-overridable (`BRUTEX_TOKEN_PARAM_ACCESS_TOKEN`, `BRUTEX_TOKEN_AWS_REGION`,
> `AWS_REGION`) but every default already matches what the minter writes — you should
> not need to set them.

---

## 4. Verify it works

Three checks, top to bottom.

### 4a. The first mint already ran — check its PASS line

You do **not** trigger the first mint by hand any more: `./deploy.sh` invoked the
Lambda itself right after the apply (step 6/6, `DEC-DEPLOY-AUTO-FIRST-MINT-001`)
and printed the verdict:

```
FIRST MINT: PASS — {"ok": true, "attempts": 1, "token_param": ".../access-token"}
```

If it printed `FIRST MINT: FAIL`, the deploy exited **non-zero** with the exact
error — fix the cause (usually a wrong/missing credential param, §1c) and re-run
`./deploy.sh`. To re-mint on demand later (idempotent — the Lambda is the sole
writer):

```bash
cd deploy/aws_token_minter
aws lambda invoke --region ap-south-1 \
  --function-name "$(terraform output -raw minter_lambda_name)" \
  /dev/stdout
```

A success prints `{"ok": true, "attempts": N, "token_param": ".../access-token"}`.

### 4b. Confirm the token landed in Parameter Store

The Lambda writes `/tickvault/prod/groww/access-token`. Confirm it now **has a
value** (this decrypts and prints the token — run it somewhere private):

```bash
aws ssm get-parameter --region ap-south-1 \
  --name /tickvault/prod/groww/access-token --with-decryption \
  --query "Parameter.{LastModified:LastModifiedDate,Version:Version}" --output table
```

A non-empty `Version` and a recent `LastModified` means the mint wrote successfully.
(Drop the `--query` to see the token value itself if you must.)

Confirm the **CloudWatch success metric** was emitted:

```bash
aws cloudwatch get-metric-statistics --region ap-south-1 \
  --namespace BruteX/GrowwTokenMinter --metric-name TokenMintSuccess \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time   "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum --output table
```

A `Sum` of `1` (or more) in the last hour means the mint reported success.

### 4c. Confirm BruteX reads it

With the read-only AWS identity configured (step 3a), run a small BruteX pull.
BruteX authenticates from Parameter Store **unconditionally** (no flag — step 3b;
DEC-AWS-ONLY-CREDS-001 #1065) — no local mint. If it authenticates and fetches
candles, the read path works.

---

## 5. The daily lifecycle (what happens on its own)

Once deployed, this runs itself — **zero clicks per day** (project rules §11):

1. **06:00 IST** — Groww's daily access token resets (verified from production logs
   2026-06-23; project rules §6 "Token lifetime").
2. **~06:05 IST** — the EventBridge schedule fires the Lambda. It reads the two
   creds, computes the TOTP, mints a fresh token, and overwrites `access-token`.
   (A transient Groww issuance throttle is retried with backoff inside the handler.)
3. **During the day** — BruteX and TickVault both read that one fresh token
   read-only. Neither mints. The system self-heals every morning with no operator
   action.
4. **If a mint ever fails** — no `TokenMintSuccess` datum lands in the 24h window, so
   the **stale-token alarm fires** and SNS emails you; a hard Lambda error trips the
   **errors alarm** immediately. You act only when paged.

> A single missed daily mint is exactly what the alarm is designed to catch (the
> alarm uses `FILL(m1, 0)` metric-math so it does not flap on a healthy day — see
> `docs/runbooks/aws-token-minter.md` §7 and AWS reference [3]).

---

## 6. Troubleshooting

| Symptom | Likely cause → fix |
|---------|--------------------|
| **BruteX: "no token in Parameter Store" / the `access-token` param does not exist** | The minter has never run. **Deploy the minter** (§2) — `./deploy.sh` triggers the first mint itself as its final step (`FIRST MINT: PASS`, §4a) and exits non-zero if that mint failed, so a completed deploy means the token exists. If a past deploy failed at the mint step, fix the cause and re-run `./deploy.sh` (or invoke once by hand, §4a). |
| **`terraform apply`: `ParameterAlreadyExists` / `EntityAlreadyExists` (409) on the token param / IAM roles / log group / Lambda** | The resources exist in AWS but the remote state does not know them (a stack first deployed under the OLD local state, or created out-of-band). `./deploy.sh` step 4 now **auto-adopts** all 7 name-fixed resources (`terraform import`, idempotent — see §2 and §2b) — re-run `./deploy.sh`. If an import itself fails, the script stops with the verbatim Terraform error; fix that and re-run. |
| **`AccessDenied` reading the token from BruteX** | The BruteX machine's AWS identity lacks `ssm:GetParameter` **or** `kms:Decrypt` on the token. Attach the `reader-brutex` role's policy (`terraform output reader_brutex_role_arn`) — it grants exactly those two, nothing else. |
| **`AccessDenied` on `kms:Decrypt` / `KMSAccessDeniedException`** | The identity can `GetParameter` but not decrypt the SecureString. A SecureString read needs BOTH `ssm:GetParameter` and `kms:Decrypt` on `alias/tickvault-groww` (AWS reference [1]). Grant `kms:Decrypt` on that key. |
| **`ParameterNotFound` for the token, but the api-key/totp-secret exist** | You are pointed at the wrong region, or the minter has not run yet. Check `aws configure get region` is `ap-south-1` and that §4a printed a success. |
| **Reader/minter reads the wrong (or empty) region** | Region mismatch. BruteX resolves region as `BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → `ap-south-1` default. Set `AWS_REGION=ap-south-1` (or `BRUTEX_TOKEN_AWS_REGION=ap-south-1`) and re-run. The minter/Terraform region is `-var 'region=ap-south-1'` (the default). |
| **`./deploy.sh: Permission denied`** | The script is not executable. Run `chmod +x deploy.sh` (or `bash deploy.sh …`). |
| **`terraform: command not found` inside IntelliJ/PyCharm but works in the Mac Terminal** | IntelliJ's built-in terminal often does not inherit your login shell's `PATH`, so Homebrew's `/opt/homebrew/bin` (Apple silicon) or `/usr/local/bin` (Intel) is missing. Fix: run `./deploy.sh` from the **macOS Terminal.app**, or add Homebrew's bin to the IDE terminal's `PATH` (IntelliJ → Settings → Tools → Terminal → *Environment variables*: `PATH=/opt/homebrew/bin:$PATH`), or invoke Terraform by absolute path. |
| **`error minting: pyotp ImportError` in the Lambda logs** | The deploy bundle was not staged. `./deploy.sh` runs `build_lambda_package.sh`, which pip-installs `pyotp` (a pure-Python universal wheel — no Docker, no cross-compile) into the zip for you — re-run `./deploy.sh` (not a bare `terraform apply`) so the bundle is rebuilt with `pyotp` included. |
| **`terraform apply`: `reading KMS Key (alias/tickvault-groww): couldn't find resource`** | The custom KMS key does not exist yet. This stack **references** it (never creates it — ADR-0014 §5). Create it once: §1d (`aws kms create-key` + `create-alias alias/tickvault-groww`), then re-run `./deploy.sh` — its pre-flight prints these commands for you. |
| **Lambda logs `KMSAccessDeniedException` on the cred read** | By default this should NOT happen — the minter is granted SSM-scoped `kms:Decrypt` on the default `alias/aws/ssm` key **and** the custom key. It only occurs if you deployed with `-var 'allow_ssm_default_key_decrypt=false'` while a cred still lives under a key the minter cannot decrypt. Fix: either drop that `-var` (re-`./deploy.sh`) so the SSM-scoped default-key grant is restored, **or** re-put both creds under `alias/tickvault-groww` (§1c, `--key-id alias/tickvault-groww --overwrite`). |
| **Never got the alarm email** | You did not confirm the SNS subscription. AWS emailed a confirmation link to `alert_email` after `apply` — click it. Re-subscribe with `aws sns subscribe --topic-arn "$(terraform output -raw alerts_topic_arn)" --protocol email --notification-endpoint you@example.com`. |

---

## 7. Rotating the Groww credentials

When your Groww API key or TOTP secret changes, **overwrite the two SecureStrings —
nothing else changes.** No redeploy, no code change; the next scheduled mint picks up
the new values automatically.

```bash
aws ssm put-parameter --region ap-south-1 --type SecureString --overwrite \
  --key-id alias/tickvault-groww \
  --name /tickvault/prod/groww/api-key   --value 'NEW_GROWW_API_KEY'

aws ssm put-parameter --region ap-south-1 --type SecureString --overwrite \
  --key-id alias/tickvault-groww \
  --name /tickvault/prod/groww/totp-secret --value 'NEW_GROWW_TOTP_SECRET'
```

Then force one fresh mint immediately instead of waiting for 06:05 IST (§4a), and
confirm the new token landed (§4b). The `access-token` param is overwritten in place;
readers pick it up on their next read.

> To tear the whole thing down, see `docs/runbooks/aws-token-minter.md` §8
> (`terraform destroy`) — it removes the Lambda/schedule/alarms/roles/token, but
> leaves your two credential SecureStrings and the KMS key untouched.

---

## AWS references

Every AWS behavior above is cited to official AWS documentation (project rules §2
Golden Rule #1 — no hallucination):

1. SSM `GetParameter` with `WithDecryption` (a SecureString read needs `ssm:GetParameter`
   **and** `kms:Decrypt`):
   <https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetParameter.html>
2. SSM `PutParameter` with `Overwrite` / `Type=SecureString` / `KeyId` (last-writer-wins overwrite):
   <https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_PutParameter.html>
3. CloudWatch alarm `TreatMissingData` + `FILL(<metric>, <value>)` metric-math for a
   sparse/daily metric (the no-flapping daily-job treatment):
   <https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html#alarms-and-missing-data>
4. EventBridge scheduled rules invoking Lambda (schedule + overlap semantics):
   <https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-run-lambda-schedule.html>
5. IAM `cloudwatch:namespace` condition key for `PutMetricData` (no resource-level permissions):
   <https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatch.html>
6. SSM SecureString envelope encryption with KMS:
   <https://docs.aws.amazon.com/kms/latest/developerguide/services-parameter-store.html>
7. AWS CLI install / configure:
   <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>
8. Terraform install:
   <https://developer.hashicorp.com/terraform/install>

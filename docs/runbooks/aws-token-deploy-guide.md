# Operator deploy guide — AWS centralized Groww token system

> **Decision:** `DEC-AWS-DEPLOY-GUIDE-001` (MIGRATION.md #1067).
> **Audience:** the operator standing up the whole centralized-token system for the
> first time. Plain English, copy-paste commands, in order.
> **Companion docs (do not duplicate — read for detail):**
> `docs/runbooks/aws-token-minter.md` (the minter's exact paths/schedule/rollback,
> DEC-TOKEN-MINTER-CENTRAL-001 / ADR-0014) and `docs/runbooks/aws-token-broker.md`
> (BruteX's read-only Parameter-Store reader, PR #1015 — present once that PR merges).

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
exist as **SecureStrings** at these exact paths, encrypted by the KMS key
`alias/tickvault-groww`:

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
  --key-id alias/tickvault-groww \
  --name /tickvault/prod/groww/api-key   --value 'YOUR_GROWW_API_KEY'

aws ssm put-parameter --region ap-south-1 --type SecureString \
  --key-id alias/tickvault-groww \
  --name /tickvault/prod/groww/totp-secret --value 'YOUR_GROWW_TOTP_SECRET'
```

> The `access-token` parameter is **not** something you create — the Lambda creates
> and writes it. Only these two credentials are your responsibility.

### 1d. The KMS key exists

The two SecureStrings are encrypted by `alias/tickvault-groww`. Confirm it resolves:

```bash
aws kms describe-key --region ap-south-1 --key-id alias/tickvault-groww \
  --query "KeyMetadata.{KeyId:KeyId,State:KeyState}" --output table
```

You want `State = Enabled`. (If you use a different key alias, pass it later with
`-var 'kms_key_alias=alias/your-key'`.)

---

## 2. The one-time deploy

One command builds the Lambda bundle and provisions everything. It takes ~2 minutes.

```bash
cd deploy/aws_token_minter
./deploy.sh -var 'alert_email=you@example.com'
#   or:  ALERT_EMAIL=you@example.com ./deploy.sh
```

**What `./deploy.sh` does, in order** (it never auto-approves — see below):

1. **Builds the Lambda bundle** (`build_lambda_package.sh`) — pip-installs `pyotp`
   into the deploy zip. That is the ONLY bundled dependency; it is a pure-Python
   universal wheel (zero platform-specific files), so the bundle imports cleanly on
   the Lambda's x86_64 Linux / Python 3.12 runtime **no matter what you build on**
   — including an Apple-silicon Mac (project rules §8) — with **no Docker**. The
   Groww token is minted by a pure-stdlib HTTPS POST in the handler (no `growwapi`),
   which is what removed the old cross-compile/Docker requirement
   (DEC-MINTER-PURE-PYTHON-001). This step needs `pip` + PyPI network access on your
   machine.
2. **`terraform init`** — downloads the AWS/archive/null providers.
3. **`terraform apply`** — Terraform **shows you the full plan and stops, waiting for
   you to type `yes`.** Nothing is created until you confirm. This script **never**
   passes `-auto-approve` — a human confirms every apply.

**What it creates** (all in `ap-south-1`, all least-privilege):

- the **minter Lambda** `groww-token-minter` (serialized: `reserved_concurrent_executions = 1`);
- the `access-token` **SSM SecureString** parameter it writes;
- an **EventBridge schedule** `cron(35 0 * * ? *)` = 00:35 UTC = **06:05 IST**;
- a **stale-token CloudWatch alarm** (pages if no successful mint in 24h) + a
  **Lambda-errors alarm** (pages on any hard failure) + the **SNS topic** they publish to;
- **three least-privilege IAM roles** — the minter (read the 2 creds, write the
  token, decrypt via KMS), plus `reader-tickvault` and `reader-brutex` (each can
  ONLY `GetParameter` the token + `kms:Decrypt`, nothing else).

**After `apply` succeeds, confirm the SNS email subscription.** AWS emails a
confirmation link to `alert_email`; the alarm cannot page you until you click it.

Read the plan before you type `yes` — you should see roughly one Lambda, one SSM
parameter, one EventBridge rule, two alarms, one SNS topic, and three IAM roles being
**added**, and **zero** to change or destroy on a fresh account.

> Want to override a default (different account prefix, KMS alias, schedule)? Pass
> extra `-var` flags straight through — see `docs/runbooks/aws-token-minter.md` §4.

---

## 3. How BruteX reads it

BruteX reads the token **read-only** from Parameter Store. Two settings on the
machine that runs the pull:

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

### 3b. Turn on the read-only broker, then pull

Set the toggle so BruteX resolves the shared token from Parameter Store instead of
minting locally, then run a pull:

```bash
export BRUTEX_TOKEN_BROKER_READONLY=true
# region defaults to ap-south-1; override only if your token lives elsewhere:
# export BRUTEX_TOKEN_AWS_REGION=ap-south-1

# ...then run your normal BruteX pull (dashboard or CLI).
```

**Why the env var:** with `BRUTEX_TOKEN_BROKER_READONLY=true` BruteX **reads** the
token from `/tickvault/prod/groww/access-token` and **never mints** — a mint attempt
is a loud fail-fast, never a silent local mint. **Unset / blank / any other value ⇒
BruteX falls back to the local on-disk cache + local TOTP mint** (byte-identical to
the pre-broker behavior), so local dev and CI need no AWS at all. This toggle exists
because BruteX is not yet AWS-only; a later change will make read-only the default and
retire the flag. (See PR #1015 / `docs/runbooks/aws-token-broker.md` for the reader's
full contract.)

> The token param path, the region, and the credential paths are all
> env-overridable (`BRUTEX_TOKEN_PARAM_ACCESS_TOKEN`, `BRUTEX_TOKEN_AWS_REGION`,
> `AWS_REGION`) but every default already matches what the minter writes — you should
> not need to set them.

---

## 4. Verify it works

Three checks, top to bottom.

### 4a. Trigger one mint on demand (don't wait for 06:05)

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

With `BRUTEX_TOKEN_BROKER_READONLY=true` set (step 3b) and the read-only AWS identity
configured, run a small BruteX pull. It authenticates using the token from Parameter
Store — no local mint. If it authenticates and fetches candles, the read path works.

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
| **BruteX: "no token in Parameter Store" / the `access-token` param does not exist** | The minter has never run. **Deploy the minter first** (§2), then invoke it once (§4a). Until the Lambda writes it, the token param does not exist. |
| **`AccessDenied` reading the token from BruteX** | The BruteX machine's AWS identity lacks `ssm:GetParameter` **or** `kms:Decrypt` on the token. Attach the `reader-brutex` role's policy (`terraform output reader_brutex_role_arn`) — it grants exactly those two, nothing else. |
| **`AccessDenied` on `kms:Decrypt` / `KMSAccessDeniedException`** | The identity can `GetParameter` but not decrypt the SecureString. A SecureString read needs BOTH `ssm:GetParameter` and `kms:Decrypt` on `alias/tickvault-groww` (AWS reference [1]). Grant `kms:Decrypt` on that key. |
| **`ParameterNotFound` for the token, but the api-key/totp-secret exist** | You are pointed at the wrong region, or the minter has not run yet. Check `aws configure get region` is `ap-south-1` and that §4a printed a success. |
| **Reader/minter reads the wrong (or empty) region** | Region mismatch. BruteX resolves region as `BRUTEX_TOKEN_AWS_REGION` → `AWS_REGION` → `ap-south-1` default. Set `AWS_REGION=ap-south-1` (or `BRUTEX_TOKEN_AWS_REGION=ap-south-1`) and re-run. The minter/Terraform region is `-var 'region=ap-south-1'` (the default). |
| **`./deploy.sh: Permission denied`** | The script is not executable. Run `chmod +x deploy.sh` (or `bash deploy.sh …`). |
| **`terraform: command not found` inside IntelliJ/PyCharm but works in the Mac Terminal** | IntelliJ's built-in terminal often does not inherit your login shell's `PATH`, so Homebrew's `/opt/homebrew/bin` (Apple silicon) or `/usr/local/bin` (Intel) is missing. Fix: run `./deploy.sh` from the **macOS Terminal.app**, or add Homebrew's bin to the IDE terminal's `PATH` (IntelliJ → Settings → Tools → Terminal → *Environment variables*: `PATH=/opt/homebrew/bin:$PATH`), or invoke Terraform by absolute path. |
| **`error minting: pyotp ImportError` in the Lambda logs** | The deploy bundle was not staged. `./deploy.sh` runs `build_lambda_package.sh`, which pip-installs `pyotp` (a pure-Python universal wheel — no Docker, no cross-compile) into the zip for you — re-run `./deploy.sh` (not a bare `terraform apply`) so the bundle is rebuilt with `pyotp` included. |
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

# Runbook — Data Fetch Run-Day (instruments · expiries · contracts · index candles)

> **Target audience:** the operator (sjparthi) on the day Groww access is
> first enabled, and any Claude session driving the first real ingest.
> **Time required:** ~10 min operator setup + hours for the first full index
> pull (1-minute tiles in 30-day vendor chunks per CLAUDE.md §6); minutes for
> incremental re-runs.
> **Prerequisites:** active Groww trade-api access, the AWS-SSM-seeded
> credentials (DEC-AWS-ONLY-CREDS-001 — see §1), and an environment whose
> **network policy allows the Groww hosts**.

This runbook is the single-paste recipe for the operator directive
(2026-05-29): *"pulling entire instruments, totp, all expiries, all
contracts, and current-year NIFTY 50 / BANKNIFTY / SENSEX all timeframes."*

It honors CLAUDE.md §2 rule 6 (rate-limit safety), §6 (verified vendor
facts), and Golden Rule #1 (no hallucination — every value below is
traceable to the cited file).

> **✅ Live-CLI verified (2026-07-03):** the `fetch` flag list + the §3–§4
> runtime behaviour in this runbook were captured from
> `python -m brutex --help` / `python -m brutex fetch --help` and exercised
> end-to-end (`fetch --dry-run`) on the branch-base commit (`3914b478…`) — see
> `## Verification evidence` below. This supersedes the
> "§3–§4 flags pending re-verification" banner of DEC-DHAN-DOC-RECONCILE-001
> (2026-06-02). The authoritative LIVE-pull surface remains the dashboard
> **`/universe` "Pull · full pipeline"** button (DEC-ONE-CLICK-PIPELINE-001
> #872 / DEC-PIPELINE-AUTOPREBUILD-001 #1145); the `/credentials` page named
> by the old banner was REMOVED — credentials are AWS-SSM-only
> (DEC-AWS-ONLY-CREDS-001).

---

## 0. Why this is blocked today (the honest envelope)

As of 2026-05-29 the fetch **cannot run inside the web environment** — proven
by three checks, two of which only the operator can clear:

| Wall | Proof | Who clears it |
|------|-------|---------------|
| Network policy denies Groww hosts | a `curl -I` against the Groww instrument-master / API host returns `HTTP 403 host_not_allowed` when blocked | **operator** (environment network policy) |
| No Groww credentials | AWS SSM parameters not seeded / AWS credentials not usable (DEC-AWS-ONLY-CREDS-001) | **operator** (`aws configure`; seed the SecureStrings + deploy `deploy/aws_token_minter/deploy.sh`) |
| Runtime deps not installed | no `.venv`; `brutex` import fails | Claude / `make setup` (PyPI is reachable) |

PyPI itself **is** reachable (`curl -I https://pypi.org/simple/` → `HTTP 200`),
so only the Groww road and the keys are operator-side.

---

## 1. Operator unblock checklist (do these once)

1. **Enable** Groww trade-api access and generate a **Groww API key**
   + note your **TOTP shared secret** from the Groww trade-api console
   (CLAUDE.md §6 — auth is TOTP: the API key plus the shared secret mint a
   6-digit per-session code, which meets §11 AUTOMATED for unattended runs).

2. **Allow the Groww hosts in the environment network policy.** The web
   environment is "deny by default"; add the Groww instrument-master and
   API hosts the adapter calls.
   See https://code.claude.com/docs/en/claude-code-on-the-web for how
   network policies are chosen at environment-creation time.

3. **Seed the credentials into AWS Parameter Store** (never commit them, never
   env vars — DEC-AWS-ONLY-CREDS-001; the old dashboard `/credentials` page was
   REMOVED). Store the api-key + TOTP secret as SecureStrings at
   `/tickvault/prod/groww/api-key` / `/tickvault/prod/groww/totp-secret`
   (region `ap-south-1`), deploy the Lambda token minter that owns
   `/tickvault/prod/groww/access-token` (`deploy/aws_token_minter/deploy.sh`),
   then give the pull box usable AWS credentials (`aws configure` /
   `AWS_PROFILE` — read-only `ssm:GetParameter` + `kms:Decrypt`).
   Full var reference: `docs/runbooks/ENV_VARS.md` §5; broker setup:
   `docs/runbooks/aws-token-broker.md`.

4. (Optional) **Arm Telegram alerts** so the run reports to your phone (all
   three vars are required when enabled — `notifications/telegram.py:91-93`):
   ```
   BRUTEX_TELEGRAM_ENABLED=true
   BRUTEX_TELEGRAM_BOT_TOKEN=<bot token>
   BRUTEX_TELEGRAM_CHAT_ID=<numeric chat id>
   ```
   Setup: `docs/runbooks/telegram-setup.md`; what each alert means:
   `docs/runbooks/critical-alerts.md`.

---

## 2. One-time environment build (Claude or operator)

```bash
make setup          # creates .venv, installs .[dev,data,ui], builds fno_math
# or, minimal for a fetch-only box:
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[data,ui]"
```

`make setup` is the single source of truth (CLAUDE.md §11 COMMON) — see
DEC-CLONE-RUN-002 (MIGRATION #297).

---

## 3. The fetch surfaces → exact commands

| Surface you asked for | Command | Notes / source |
|-----------------------|---------|----------------|
| **Prove the pipe (no creds)** | `.venv/bin/python -m brutex fetch --dry-run` | synthetic candles → real Parquet sink into an EPHEMERAL temp lake (production lake untouched); offline smoke |
| **Probe Groww data shape** | `.venv/bin/python -m brutex fetch --inspect` | flag parses, but the runtime prints "`--inspect` is not yet ported to the Groww adapter — running to a NullBarSink (no persistence)" (`cli.py:2916-2921`); the parser-help's per-timeframe-counts report is NOT produced today |
| **Entire instrument master** | `.venv/bin/python -m brutex fetch --instruments` | keyless public CSV — loads the master only, prints counts, exits (no candles) |
| **All expiries + all contracts** | (dashboard pipeline — see §4) | metadata phase of `/universe` "Pull · full pipeline" (Groww `get_expiries` → `get_contracts`) |
| **NIFTY 50 · BANKNIFTY · SENSEX, full history** | `.venv/bin/python -m brutex fetch` | `BRUTE_FORCE_INDICES` = `NSE-NIFTY, NSE-BANKNIFTY, BSE-SENSEX`; defaults below |

Defaults for the index pull (no flags needed for the run-day ask):
- **symbols:** the 3 brute-force indices (`brutex/universe.py:118`
  `BRUTE_FORCE_INDICES`)
- **interval:** `all` → `1minute` + `1day`, BOTH vendor-FETCHED on this
  vestigial CLI path (`cli.py:2267` `_resolve_fetch_intervals` — its own
  comment: the DASHBOARD pipeline is what fetches 1-minute ONLY and DERIVES
  `1day`, DEC-1MINUTE-ONLY-DERIVE-1DAY-001; `brutex fetch` never derives
  mid-pull). `--interval` accepts exactly `1minute` / `1day` / `1m` / `1d` /
  `all`; anything else errors. The 19 intraday TFs are DERIVED from the
  stored 1-minute, never vendor-fetched — NOT "every native Groww timeframe".
- **start:** `2020-01-01` — the pull floor `GROWW_HISTORY_FLOOR`
  (`pull_controller.py:137`; DEC-PULL-FLOOR-2020-001), applied at
  `cli.py:2886-2893`. The `--start` parser help still says "Jan 1 of this
  year" (`cli.py:560`) — a known help-text drift (code nit, tracked
  separately); the CODE default is the 2020 floor.
- **end:** now, UTC
- **chunking:** 1-minute tiles in 30-day vendor chunks (CLAUDE.md §6 interval
  tier table). `--window-days` is accepted but IGNORED on Groww
  (`cli.py:2894-2899`).
- **rate limit:** the shared `RateController` paces at the production
  **8 r/s** ceiling (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`,
  DEC-GROWW-RATE-CEILING-8-002; AIMD steps down toward soft floor 3 / deep
  floor 1 on 429s) under the operator-confirmed **500 req/min HARD governor
  — there is NO daily quota** (DEC-GROWW-RATE-PERMIN-001, 2026-06-13). The
  per-second backtesting cap itself remains vendor-UNVERIFIED (CLAUDE.md §2
  Rule 6).

---

## 4. Universe ingest (instruments + expiries + contracts)

> **⚠️ `fetch --universe` is RETIRED — runtime-refused.** The flag still
> *parses* (the parser help at `cli.py:598-611` still describes the old
> three-phase ingest — a known help-text drift, tracked as a code nit), but
> `cmd_fetch` refuses it and exits `EXIT_USER_ERROR` with:
>
> > `--universe is retired (legacy three-phase ingest removed); use `brutex
> > fetch --instruments` to load the master, then the /universe dashboard
> > buttons to pull spot + options`
>
> (`cli.py:2901-2914`; the auto-resume argv validator likewise rejects it.)

The replacement paths:

```bash
# Instrument master ONLY — keyless public CSV, no vendor API keys, no candles:
.venv/bin/python -m brutex fetch --instruments
```

For the **full universe** — spot 1-minute → expiries+contracts metadata →
F&O historical candles → the ONE automatic FINAL timeframe-prebuild stage —
use the dashboard **`/universe` "Pull · full pipeline"** button
(`POST /api/v1/universe/pull-pipeline`; DEC-ONE-CLICK-PIPELINE-001 #872,
DEC-PIPELINE-AUTOPREBUILD-001 #1145). The metadata phase resolves expiries +
contracts via Groww `get_expiries` → `get_contracts`
(`adapters/groww/contracts.py`).

Scope (Golden Rule #3): this populates the **LAKE only** — the brute-force
ENGINE stays the 3 indices; the discovered F&O universe is `REFERENCE_ONLY`
(DEC-P0-063, MIGRATION #200).

---

## 5. Verify the pull

```bash
.venv/bin/python -m brutex status          # pipeline phase + run_id
.venv/bin/python -m brutex doctor          # audit hash-chain + runtime health
.venv/bin/python -m brutex dashboard       # FastAPI UI to eyeball the lake
ls -R ~/.brutex/lake                        # Parquet partitions per instrument/TF
```

The SessionStart hook (DEC-SESSION-HOOK-LIVESTATE-001) now surfaces
pipeline status + audit-log count + Telegram arm-state automatically on
every new Claude session, so you see live state with zero clicks.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `HTTP 403 host_not_allowed` | network policy still denies Groww | re-check §1 step 2 |
| auth failure / `HTTP 401` | SSM api-key / totp-secret parameter wrong or unseeded, or the AWS reader lacks `ssm:GetParameter` + `kms:Decrypt` (the per-session TOTP code could not be minted/exchanged) | §1 step 3; re-check the SecureStrings + the IAM policy (`docs/runbooks/aws-token-broker.md` §2) |
| `ModuleNotFoundError` on import | `[data,ui]` extras not installed | `uv pip install --python .venv -e ".[data,ui]"` |
| `ModuleNotFoundError: fno_math` (only on engine paths) | Rust kernel not built | `make setup`, or `cd rust/fno-math && maturin develop --release` |
| Rate-limit exception (`HTTP 429`) | burst over the vendor's tolerance at the 8 r/s production ceiling (CLAUDE.md §2 Rule 6) | the AIMD `RateController` auto-steps-down toward soft floor 3 / deep floor 1 and recovers once the storm clears; retry |
| INDIA VIX missing from CSV | Phase 0 HARD-GATE probe failure | escalate — CLAUDE.md §6 (INDIA VIX presence UNVERIFIED) |

---

## 6a. One pull at a time per lake — and the two-lake-root residual

**DEC-PULL-HARDENING-FOLLOWUP-001 (MIGRATION.md #996).** Only ONE pull may run at a
time per `BRUTEX_LAKE_ROOT`. A cross-process single-flight lock
(`<lake_root>/.locks/pull_singleflight.lock`, an advisory `flock`) is taken by BOTH
entrypoints — the dashboard pull **and** the CLI `brutex fetch` — so starting a second
pull against the SAME lake fails fast with:

> `a pull is already running for this lake … refusing to start a second concurrent pull`

This protects the Groww **account** request rate (CLAUDE.md Golden Rule #6): two
concurrent pulls would each pace at the 8 r/s ceiling, so the account would see ~16 r/s
and risk a sustained 429 storm. `--dry-run` / `--inspect` do not take the lock (they
write no real data and pace no real account requests).

> **⚠️ RESIDUAL — the lock is keyed by LAKE ROOT, not by Groww ACCOUNT.** If you point
> the SAME Groww account at **two different `BRUTEX_LAKE_ROOT`s** and start a pull in
> each, the two take DIFFERENT lock files, BOTH acquire, and the account STILL sees the
> SUM of both rates. The guard will NOT stop you. **Operator rule: never run two pulls
> on one Groww account at the same time, even into different lake roots.** Keying the
> lock on the credentials / feed-token is a flagged follow-up, not yet implemented.

---

## 7. Verification evidence (2026-07-03, branch-base capture)

Captured on the branch-base commit `3914b478…` with a python-3.12 venv +
locally-built `trading_core` / `fno_math` wheels (`PYTHONPATH=<repo root>`):

- `python -m brutex --help` → subcommands
  `{version,status,audit,catalog,reopen-skipped,auth,dashboard,run,fetch,
  doctor,monitor,notify,wipe,sweep,sweep-all,sweep-daemon,supervisor,results,
  options-sim,cascade,verify-lookahead,sweep-verify,sweep-heal}`.
- `python -m brutex fetch --help` → exactly the flags documented in §3–§4
  (`--lake-root --start --end --interval --window-days --dry-run --inspect
  --rows-per-chunk --universe --instruments --no-auto-retry --retry-failed
  --until-clean --until-clean-rounds --max-retry-minutes`).
- `fetch --dry-run` (scratch lake) → **windows=282, bars_appended=1410,
  failed_pairs=0, transient=0** across NIFTY/BANKNIFTY/SENSEX
  (`1minute` + `1day` pairs), closing line
  "`dry-run — production lake untouched`" (the ephemeral-lake isolation of
  DEC-DRYRUN-LAKE-ISOLATION-001).
- The §3 runtime-truth claims (`--universe` refusal, `--inspect` NullBarSink
  notice, `--window-days` ignored, the 2020-01-01 start floor) were verified
  against `brutex/cli.py` on the same commit (line anchors cited inline).

---

## 8. Decision trail

- **Network/creds blocker + unblock path:** this runbook + DEC-SESSION-HOOK-LIVESTATE-001 (MIGRATION #298).
- **Cross-process pull single-flight (per lake) + CLI wiring + two-lake-root residual:** DEC-PULL-HARDENING-FOLLOWUP-001 (MIGRATION #996); `brutex/adapters/groww/pull_singleflight.py`, `brutex/cli.py` (`_enter_pull_singleflight`).
- **Index symbols + scope:** Golden Rule #3; `brutex/universe.py:118` (`BRUTE_FORCE_INDICES`).
- **Rate-limit + chunk windows:** CLAUDE.md §2 Rule 6 + §6 (8 r/s ceiling, 500 req/min governor, NO daily quota).
- **Auth (AWS-SSM-only credentials + shared token broker):** DEC-AWS-ONLY-CREDS-001 (MIGRATION #1065); `brutex/config/credentials_aws.py`, `brutex/adapters/groww/token_store_aws.py`; CLAUDE.md §6.
- **Universe expansion to F&O lake:** DEC-P0-063 (MIGRATION #200).

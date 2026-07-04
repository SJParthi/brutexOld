# Runbook — Data Fetch Run-Day (instruments · expiries · contracts · index candles)

> **Target audience:** the operator (sjparthi) on the day Groww access is
> first enabled, and any Claude session driving the first real ingest.
> **Time required:** ~10 min operator setup + ~1-2 h first full index pull
> (90-day intraday chunks per CLAUDE.md §6); minutes for incremental re-runs.
> **Prerequisites:** active Groww trade-api access, a Groww API key + TOTP
> shared secret, and an environment whose **network policy allows the Groww
> hosts**.

This runbook is the single-paste recipe for the operator directive
(2026-05-29): *"pulling entire instruments, totp, all expiries, all
contracts, and current-year NIFTY 50 / BANKNIFTY / SENSEX all timeframes."*

It honors CLAUDE.md §2 rule 6 (rate-limit safety), §6 (verified vendor
facts), and Golden Rule #1 (no hallucination — every value below is
traceable to the cited file).

> **⚠️ Reconciliation banner (DEC-DHAN-DOC-RECONCILE-001, 2026-06-02):** the
> live vendor is **Groww** (DEC-GROWW-DHAN-REVERT-001, ADR-0002).
> The §0–§2 + §6 auth / host / setup steps below are Groww-correct. The exact
> `brutex fetch` CLI flags in §3–§4 are **pending a live-CLI
> re-verification** (tracked follow-up): the authoritative live-pull path today
> is the dashboard **`/universe` "Pull the Universe"** panel + the
> **`/credentials`** page (live pulls enable from the UI; MIGRATION #261-#267).

---

## 0. Why this is blocked today (the honest envelope)

As of 2026-05-29 the fetch **cannot run inside the web environment** — proven
by three checks, two of which only the operator can clear:

| Wall | Proof | Who clears it |
|------|-------|---------------|
| Network policy denies Groww hosts | a `curl -I` against the Groww instrument-master / API host returns `HTTP 403 host_not_allowed` when blocked | **operator** (environment network policy) |
| No Groww credentials | `BRUTEX_GROWW_API_KEY` + `BRUTEX_GROWW_TOTP_SECRET` both unset | **operator** (env vars, or the dashboard `/credentials` page) |
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

3. **Set the two credentials** as environment variables (never commit them;
   the dashboard `/credentials` page can also store them):
   ```
   BRUTEX_GROWW_API_KEY=<Groww API key from the trade-api console>
   BRUTEX_GROWW_TOTP_SECRET=<your Groww TOTP shared secret>
   ```
   Full var reference: `docs/runbooks/ENV_VARS.md` §5.

4. (Optional) **Arm Telegram alerts** so the run reports to your phone:
   ```
   BRUTEX_TELEGRAM_ENABLED=true
   BRUTEX_TELEGRAM_BOT_TOKEN=<bot token>
   ```
   Setup: `docs/runbooks/telegram-setup.md`.

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
| **Prove the pipe (no creds)** | `.venv/bin/python -m brutex fetch --dry-run` | synthetic candles → real Parquet sink; offline smoke |
| **Probe Groww data shape** | `.venv/bin/python -m brutex fetch --inspect` | per-timeframe pre/regular/post-market bar counts + grid alignment (IST) |
| **Entire instrument master** | (universe ingest — see §4) | public CSV, no auth; `InstrumentMasterFetcher` (`instruments.py`) |
| **All expiries** | (universe ingest — see §4) | `ExpiryFetcher` (`expiries.py`) — F&O underlyings |
| **All contracts** | (universe ingest — see §4) | `ContractFetcher` (`contracts.py`) |
| **NIFTY 50 · BANKNIFTY · SENSEX, all timeframes, current year** | `.venv/bin/python -m brutex fetch` | `BRUTE_FORCE_INDEX_SYMBOLS` = `NSE-NIFTY, NSE-BANKNIFTY, BSE-SENSEX`; `--interval all`; default `--start` = Jan 1 this year (`cli.py:512`) |

Defaults for the index pull (no flags needed for the run-day ask):
- **symbols:** the 3 brute-force indices (`ingest.py:37`)
- **interval:** `all` → every native Groww timeframe (`chunks.ALL_GROWW_INTERVALS`)
- **start:** Jan 1 of the current year, UTC (`cli.py:512`)
- **end:** now, UTC
- **chunking:** per-interval window per CLAUDE.md §6 (1m/2m/3m/5m = 30d, 10m/15m/30m = 90d, 1h/4h/1d = 180d)
- **rate limit:** adapter caps at the §6 safety factor; the 92-chunk
  first run is < 0.1% of the 100k/day quota.

---

## 4. Universe ingest (instruments + expiries + contracts)

A single turnkey command pulls the **entire** lake surface — instrument
master + all expiries + all contracts — and then the 3 index candle series,
all in one invocation (DEC-FETCH-UNIVERSE-001, MIGRATION #299):

```bash
# LIVE (Groww-auth gated): instruments → expiries → contracts → index candles
.venv/bin/python -m brutex fetch --universe

# OFFLINE proof (no creds, no network): synthetic instruments/expiries/contracts
# through the REAL parse + sink pipeline, then synthetic index candles:
.venv/bin/python -m brutex fetch --universe --dry-run
```

`--universe` runs the LAKE-surface discovery pass **before** the candle
ingest, reusing the adapter primitives (`InstrumentMasterFetcher` →
`ExpiryFetcher` → `ContractFetcher` via `universe_ingest.run_universe_ingest`):

| Surface | Primitive | Source |
|---------|-----------|--------|
| Instrument master | `InstrumentMasterFetcher` (`instruments.py`) | public CSV, no auth |
| Expiries (per underlying / year) | `ExpiryFetcher` (`expiries.py`) | Groww `get_expiries`, FNO from 2020 |
| Contracts (per expiry) | `ContractFetcher` (`contracts.py`) | Groww `get_contracts` + master enrichment |

Scope (Golden Rule #3): this populates the **LAKE only** — the brute-force
ENGINE stays the 3 indices; the discovered F&O universe is `REFERENCE_ONLY`
(DEC-P0-063, MIGRATION #200).

> Honest envelope: the LIVE `--universe` path needs the §1 operator unblock
> (network policy + TOTP creds). The `--dry-run` path additionally needs the
> `fno_math` kernel built (`make setup`) because the DuckDB `InstrumentStore`
> imports it transitively. The pure orchestration core is unit-tested offline
> in `tests/unit/test_groww_universe_ingest.py`.

Optionally, the standalone discovery script still emits JSON inventories of
the complete NSE+BSE universe (no auth, no lake write) for a quick eyeball:

```bash
.venv/bin/python scripts/groww_universe_discovery.py
#   → docs/groww/universe/0[1-5]_*.json
```

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
| auth failure / `HTTP 401` | `BRUTEX_GROWW_API_KEY` / `BRUTEX_GROWW_TOTP_SECRET` unset or wrong (the per-session TOTP code could not be minted/exchanged) | §1 step 3; re-check the API key + TOTP secret |
| `ModuleNotFoundError` on import | `[data,ui]` extras not installed | `uv pip install --python .venv -e ".[data,ui]"` |
| `ModuleNotFoundError: fno_math` (only on engine paths) | Rust kernel not built | `make setup`, or `cd rust/fno-math && maturin develop --release` |
| Rate-limit exception (`HTTP 429`) | burst over the 5 r/s Groww cap | `rate_control` auto-backs-off 5→4→3; retry |
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

> **✅ CLOSED for the single-host case (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001, MIGRATION
> #1285) — an ACCOUNT-keyed second lock is now taken FIRST.** Both pull sites (the
> dashboard `BackgroundPullJob` and the CLI `brutex fetch`) additionally take
> `~/.brutex/locks/pull_account<sha256(api_key)[:16]>.flock`, keyed off the AWS-resolved
> api_key digest (never the key itself) — so the SAME account pointed at **two different
> `BRUTEX_LAKE_ROOT`s** now contends on ONE file and the second pull is refused.
> **Honest envelope:** the lock dir is per-OS-user and an `flock` is per-host — two OS
> users or two HOSTS pulling one account are still NOT covered (named follow-ups, as is
> keying on the vendor feed-token). A credentials-less run (dry-run / no AWS) skips the
> account lock gracefully — it makes zero vendor calls. **Operator rule unchanged for
> multi-host setups: never run two pulls on one Groww account at the same time across
> machines.**

---

## 7. Decision trail

- **Network/creds blocker + unblock path:** this runbook + DEC-SESSION-HOOK-LIVESTATE-001 (MIGRATION #298).
- **Cross-process pull single-flight (per lake) + CLI wiring + two-lake-root residual:** DEC-PULL-HARDENING-FOLLOWUP-001 (MIGRATION #996); `brutex/adapters/groww/pull_singleflight.py`, `brutex/cli.py` (`_enter_pull_singleflight`). **Account-keyed second lock (single-host residual closed):** DEC-ACCOUNT-KEY-SINGLEFLIGHT-001 (MIGRATION #1285).
- **Wedged-volume write-health breaker:** DEC-WRITE-HEALTH-BREAKER-001 (MIGRATION #1281, incl. the fix-round fold — the F&O health key groups by UNDERLYING so the shipped 2-consecutive / 3-distinct defaults can trip on both spines; a pull whose defers span fewer than 3 distinct health keys keeps per-window defers); `brutex/data/write_breaker.py`; kill switch `BRUTEX_WRITE_BREAKER=off`.
- **Index symbols + scope:** Golden Rule #3; `ingest.py:37`.
- **Rate-limit + chunk windows:** CLAUDE.md §6.
- **Auth (Groww API key + TOTP shared secret):** CLAUDE.md §6; `brutex/adapters/groww/auth.py`.
- **Universe expansion to F&O lake:** DEC-P0-063 (MIGRATION #200).

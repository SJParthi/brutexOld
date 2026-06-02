# Runbook — Data Fetch Run-Day (instruments · expiries · contracts · index candles)

> **Target audience:** the operator (sjparthi) on the day Dhan access is
> first enabled, and any Claude session driving the first real ingest.
> **Time required:** ~10 min operator setup + ~1-2 h first full index pull
> (90-day intraday chunks per CLAUDE.md §6); minutes for incremental re-runs.
> **Prerequisites:** an active Dhan Data API v2 subscription, a web-generated
> access token + client id, and an environment whose **network policy allows
> the Dhan hosts** (`api.dhan.co`, `images.dhan.co`).

This runbook is the single-paste recipe for the operator directive
(2026-05-29): *"pulling entire instruments, totp, all expiries, all
contracts, and current-year NIFTY 50 / BANKNIFTY / SENSEX all timeframes."*

It honors CLAUDE.md §2 rule 6 (rate-limit safety), §6 (verified Dhan
facts), and Golden Rule #1 (no hallucination — every value below is
traceable to the cited file).

> **⚠️ Reconciliation banner (DEC-DHAN-DOC-RECONCILE-001, 2026-06-02):** the
> live vendor is **Dhan Data API v2** (DEC-GROWW-DHAN-REVERT-001, ADR-0002).
> The §0–§2 + §6 auth / host / setup steps below are Dhan-correct. The exact
> `brutex fetch` CLI flags in §3–§4 are **Groww-era and pending a live-CLI
> re-verification** (tracked follow-up): the authoritative live-pull path today
> is the dashboard **`/universe` "Pull the Universe"** panel + the
> **`/credentials`** page (live pulls enable from the UI; MIGRATION #261-#267).

---

## 0. Why this is blocked today (the honest envelope)

As of 2026-05-29 the fetch **cannot run inside the web environment** — proven
by three checks, two of which only the operator can clear:

| Wall | Proof | Who clears it |
|------|-------|---------------|
| Network policy denies Dhan hosts | `curl -I https://images.dhan.co/api-data/api-scrip-master-detailed.csv` returns `HTTP 403 host_not_allowed` when blocked | **operator** (environment network policy) |
| No Dhan credentials | `BRUTEX_DHAN_ACCESS_TOKEN` + `BRUTEX_DHAN_CLIENT_ID` both unset | **operator** (env vars, or the dashboard `/credentials` page) |
| Runtime deps not installed | no `.venv`; `brutex` import fails | Claude / `make setup` (PyPI is reachable) |

PyPI itself **is** reachable (`curl -I https://pypi.org/simple/` → `HTTP 200`),
so only the Dhan road and the keys are operator-side.

---

## 1. Operator unblock checklist (do these once)

1. **Subscribe** to the Dhan Data API v2 and generate a **web access token**
   + note your **client id** from web.dhan.co (CLAUDE.md §6 — the default auth
   is a manual web-generated token; a TOTP `generateAccessToken` flow exists
   for unattended renewal that meets §11 AUTOMATED).

2. **Allow the Dhan hosts in the environment network policy.** The web
   environment is "deny by default"; add at least:
   - `images.dhan.co` (public instrument master CSV — no auth)
   - `api.dhan.co` (the Data API v2 host)
   See https://code.claude.com/docs/en/claude-code-on-the-web for how
   network policies are chosen at environment-creation time.

3. **Set the two credentials** as environment variables (never commit them;
   the dashboard `/credentials` page can also store them):
   ```
   BRUTEX_DHAN_ACCESS_TOKEN=<access token from web.dhan.co>
   BRUTEX_DHAN_CLIENT_ID=<your Dhan client id>
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
| `HTTP 403 host_not_allowed` | network policy still denies Dhan | re-check §1 step 2 |
| auth failure / `HTTP 401` | `BRUTEX_DHAN_ACCESS_TOKEN` / `BRUTEX_DHAN_CLIENT_ID` unset or token expired (24h) | §1 step 3; re-generate the web token |
| `ModuleNotFoundError` on import | `[data,ui]` extras not installed | `uv pip install --python .venv -e ".[data,ui]"` |
| `ModuleNotFoundError: fno_math` (only on engine paths) | Rust kernel not built | `make setup`, or `cd rust/fno-math && maturin develop --release` |
| Rate-limit exception (`HTTP 429`) | burst over the 5 r/s Dhan cap | `rate_control` auto-backs-off 5→4→3; retry |
| INDIA VIX missing from CSV | Phase 0 HARD-GATE probe failure | escalate — CLAUDE.md §6 (INDIA VIX presence UNVERIFIED) |

---

## 7. Decision trail

- **Network/creds blocker + unblock path:** this runbook + DEC-SESSION-HOOK-LIVESTATE-001 (MIGRATION #298).
- **Index symbols + scope:** Golden Rule #3; `ingest.py:37`.
- **Rate-limit + chunk windows:** CLAUDE.md §6.
- **Auth (Dhan access token + client id):** CLAUDE.md §6; `brutex/adapters/dhan/auth.py`.
- **Universe expansion to F&O lake:** DEC-P0-063 (MIGRATION #200).

# Runbook — Data Fetch Run-Day (instruments · expiries · contracts · index candles)

> **Target audience:** the operator (sjparthi) on the day Groww access is
> first enabled, and any Claude session driving the first real ingest.
> **Time required:** ~10 min operator setup + ~1-2 h first full index pull
> (92-chunk first run per CLAUDE.md §6); minutes for incremental re-runs.
> **Prerequisites:** an active Groww Trade API subscription, TOTP secret,
> and an environment whose **network policy allows the Groww hosts**.

This runbook is the single-paste recipe for the operator directive
(2026-05-29): *"pulling entire instruments, totp, all expiries, all
contracts, and current-year NIFTY 50 / BANKNIFTY / SENSEX all timeframes."*

It honors CLAUDE.md §2 rule 6 (rate-limit safety), §6 (verified Groww
facts), and Golden Rule #1 (no hallucination — every value below is
traceable to the cited file).

---

## 0. Why this is blocked today (the honest envelope)

As of 2026-05-29 the fetch **cannot run inside the web environment** — proven
by three checks, two of which only the operator can clear:

| Wall | Proof | Who clears it |
|------|-------|---------------|
| Network policy denies Groww hosts | `curl -I https://growwapi-assets.groww.in/instruments/instrument.csv` → `HTTP 403 x-deny-reason: host_not_allowed` | **operator** (environment network policy) |
| No Groww credentials | `BRUTEX_GROWW_API_KEY` + `BRUTEX_GROWW_TOTP_SECRET` both unset | **operator** (env vars) |
| Runtime deps not installed | no `.venv`; `growwapi` import fails | Claude / `make setup` (PyPI is reachable) |

PyPI itself **is** reachable (`curl -I https://pypi.org/simple/` → `HTTP 200`),
so only the Groww road and the keys are operator-side.

---

## 1. Operator unblock checklist (do these once)

1. **Subscribe** to the Groww Trade API (₹499/mo) and generate the API key
   + TOTP secret per `groww.in/trade-api/docs/python-sdk` Step 3. Use the
   **TOTP** auth approach (CLAUDE.md §6 — the API-Key+Secret approach is
   REFUSED because it needs daily Cloud-Console approval, violating §11
   AUTOMATED).

2. **Allow the Groww hosts in the environment network policy.** The web
   environment is "deny by default"; add at least:
   - `growwapi-assets.groww.in` (public instrument master CSV — no auth)
   - the Groww API host used by `growwapi` 1.5.0
   See https://code.claude.com/docs/en/claude-code-on-the-web for how
   network policies are chosen at environment-creation time.

3. **Set the two secrets** as environment variables (mode-0600 per
   `docs/TRACK1_GROWW_ADAPTER.md §2`; never commit them):
   ```
   BRUTEX_GROWW_API_KEY=<your key>
   BRUTEX_GROWW_TOTP_SECRET=<your base32 TOTP secret>
   ```
   Full var reference: `docs/runbooks/ENV_VARS.md`.

4. (Optional) **Arm Telegram alerts** so the run reports to your phone:
   ```
   BRUTEX_TELEGRAM_ENABLED=true
   BRUTEX_TELEGRAM_BOT_TOKEN=<bot token>
   ```
   Setup: `docs/runbooks/telegram-setup.md`.

---

## 2. One-time environment build (Claude or operator)

```bash
make setup          # creates .venv, installs .[dev,data,ui,groww], builds fno_math
# or, minimal for a fetch-only box:
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[data,groww]"
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

> Status: a single turnkey command (`brutex fetch --universe`) is being added
> on branch `claude/automation-coverage-strategy-Zj7HD` (DEC-SESSION-HOOK-…
> sibling work). Until merged, the universe is ingested via the standalone
> discovery script + adapter primitives:

```bash
# Public CSV → JSON inventories of the COMPLETE NSE+BSE universe (no auth):
.venv/bin/python scripts/groww_universe_discovery.py
#   → docs/groww/universe/0[1-5]_*.json
#     (all NSE indices, all BSE indices, ~200 F&O stocks, F&O indices, summary)
```

Expiries + contracts ingest into the lake are library-level
(`ExpiryFetcher` / `ContractFetcher`) and run live (Groww-auth gated);
they are exercised by the turnkey command once it lands.

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
| `KeyError: BRUTEX_GROWW_API_KEY` | secrets not exported | §1 step 3 |
| `ModuleNotFoundError: growwapi` | `[groww]` extra not installed | `uv pip install --python .venv -e ".[groww]"` |
| `ModuleNotFoundError: fno_math` (only on engine paths) | Rust kernel not built | `make setup`, or `cd rust/fno-math && maturin develop --release` |
| Rate-limit exception | burst over §6 cap | adapter auto-backs-off; lower concurrency, retry |
| INDIA VIX missing from CSV | Phase 0b HARD GATE failure | escalate — DEC-VENDOR-PIVOT-001 rescind path (CLAUDE.md §6) |

---

## 7. Decision trail

- **Network/creds blocker + unblock path:** this runbook + DEC-SESSION-HOOK-LIVESTATE-001 (MIGRATION #298).
- **Index symbols + scope:** Golden Rule #3; `ingest.py:37`.
- **Rate-limit + chunk windows:** CLAUDE.md §6.
- **Auth (TOTP only):** CLAUDE.md §6; `client.py:307-334`.
- **Universe expansion to F&O lake:** DEC-P0-063 (MIGRATION #200).

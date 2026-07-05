# Runbook — Un-settle Option Known-Gap (`SKIPPED`) Ledger Rows

> **Target audience:** the operator (sjparthi) or any Claude session recovering
> from a mass mis-settle of no-data option windows in the unified window
> ledger. **Time required:** minutes. **Prerequisites:** the lake root path
> (`$BRUTEX_LAKE_ROOT`, default `~/.brutex/lake` — `brutex/config/paths.py:50-69`)
> and the project venv.

## When you need this

A clean-but-EMPTY vendor answer (an HTTP 200 with zero candles) on an option
fetch window, once confirmed `EMPTY_TERMINAL_THRESHOLD` (= 3,
`brutex/data/window_ledger.py:84`) times, settles that window `SKIPPED` in the
unified window ledger (`<lake_root>/window_ledger/ledger.parquet` —
`window_ledger_store.py:80-81`) and the series continues — so a fully-dead
series drains in bounded passes and is never re-fetched.

> **Legacy naming note (re-grounded 2026-07-03):** earlier revisions of this
> runbook keyed the whole detection recipe on the vendor error string
> `DH-905` and cited `groww/errors.py` for it. **`DH-905` is a prior-vendor
> error code** (the retired vendor superseded by Groww per
> DEC-DHAN-GROWW-REMIGRATE-001 / ADR-0009) — it appears NOWHERE in the
> Groww adapter and survives
> only as legacy commentary (representative, NOT exhaustive:
> `data/window_ledger.py:64,556,558`, `orchestrator/watchdog.py:403`,
> `dashboard/pull_progress.py:54,175`, `dashboard/pull_jobs.py`,
> `dashboard/templates/universe.html`). A filter
> on `"DH-905" in last_error` matches **ZERO** rows of a Groww-era ledger; the
> Groww-era settle keys on the clean-empty confirmation machinery above, and
> the honest detection key is the row **`status == "SKIPPED"`** itself.

That settle trusts the vendor's answer. A *systematic request bug* (a
mis-addressed symbol — e.g. the `&`-symbol encoding class of
DEC-GROWW-AMPERSAND-FALLBACK-001 #898 — a malformed window, a vendor contract
change) can mass-settle windows that the vendor actually HAS data for, and
every later pull then reads clean with zero vendor calls.

**Detection signals** (any of):

- a pull verdict of `known_gap_only` / "all empty" for series you KNOW traded
  (e.g. ATM NIFTY weeklies);
- `/db` ledger rows with `status == SKIPPED` across a suspiciously broad
  coordinate range;
- `pull.underlying_all_empty` WARNING lines in the pull log (the
  whole-underlying silent-vanish detector — see
  `docs/runbooks/critical-alerts.md` §3).

The fix is ledger surgery: re-open the wrongly-settled rows so the next pull
re-fetches them. **No lake data is at risk** — a known-gap settle writes
ledger state only, never bars, and re-opening is DATA-SAFE (the idempotent
keep-first sink absorbs any re-fetch).

## 1. Inspect before re-opening (Golden Rule #10 — verify, don't assume)

```bash
cd /path/to/bruteX
.venv/bin/python - <<'EOF'
import os
from pathlib import Path
from brutex.data.window_ledger_store import load_window_ledger

LAKE_ROOT = Path(os.environ.get("BRUTEX_LAKE_ROOT", str(Path.home() / ".brutex" / "lake")))
ledger = load_window_ledger(LAKE_ROOT)
gaps = [
    r
    for r in ledger.records()
    if r.status == "SKIPPED"
    and r.market == "groww-option"
    # Optionally narrow to the suspect series, e.g.:
    # and r.security_id.startswith("NSE-NIFTY")
]
print(f"{len(gaps)} option known-gap rows")
for r in gaps[:20]:
    print(r.security_id, r.interval, r.window_start, r.window_end, r.last_error)
EOF
```

The `security_id` field carries the contract `groww_symbol` (the derivative
ledger key is built by `option_ingest._clamped_window_key`:
`market="groww-option"` — futures use `"groww-future"` — with
`security_id=<contract groww_symbol>` and `interval="1minute"`), so you can
scope the surgery to exactly the poisoned series.

## 2. Re-open the poisoned rows (scoped, atomic, merge-proof)

The first-class path is the CLI (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001):

```bash
# All SKIPPED option windows (scope with --symbol / --limit as needed):
.venv/bin/python -m brutex reopen-skipped --market groww-option
# One contract only, bounded sample:
.venv/bin/python -m brutex reopen-skipped --market groww-option \
  --symbol "NSE-<contract groww_symbol>" --limit 50
```

Each matching `SKIPPED` row is demoted to a retryable `FAILED` with
`empty_streak` reset, so the next pull re-fetches it with the full fresh
confirmation budget; the stamped `REOPENED` marker makes those re-checks
exempt from the mass-empty circuit breaker (a bulk reopen can never re-trip
endlessly). The command REFUSES while a pull is running (it takes the same
single-flight lock a pull holds) — wait for the pull to finish, then re-run.

The same operation via the Python API (identical ledger semantics, useful
from a notebook — but note the CLI additionally holds the pull single-flight
lock for you; run the raw API version ONLY while no pull is running):

```bash
.venv/bin/python - <<'EOF'
import os
from pathlib import Path
from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger

LAKE_ROOT = Path(os.environ.get("BRUTEX_LAKE_ROOT", str(Path.home() / ".brutex" / "lake")))
ledger = load_window_ledger(LAKE_ROOT)
reopened = ledger.reopen_skipped(market="groww-option")  # + security_id= / limit=
print(f"re-opened {reopened} rows")
if reopened:
    save_window_ledger(ledger, LAKE_ROOT)
EOF
```

> **Why re-open instead of deleting rows:** `save_window_ledger` is a
> cross-process-locked READ-**MERGE**-WRITE (DEC-WINDOW-LEDGER-RESCUE-001) —
> a filtered `to_rows()`/`from_rows()` rebuild that simply REMOVES rows gets
> them merged straight back from the on-disk copy on save (the merge prefers
> the more-settled status). Only `reopen_skipped` demotions survive the merge
> (each re-opened key is tombstoned via `reopened_keys()`), so re-opening is
> the ONE supported un-settle path.

## 3. Worst case: reset the whole ledger

If the poisoning is too broad to scope, delete the ledger file itself. This is
safe but expensive: the next pull re-fetches **everything** not seeded back
from the lake (the cold-start `reconcile_ledger_with_lake` /
`seed_lake_success` bridge re-seeds windows the Parquet lake already holds
bars for, so spot/clean option windows are cheap; genuinely empty windows
re-earn their `SKIPPED` status over re-pulls).

```bash
rm "${BRUTEX_LAKE_ROOT:-$HOME/.brutex/lake}"/window_ledger/ledger.parquet
```

## 4. Fix the root cause, then re-pull

Re-opening rows without fixing the request bug just re-settles them. Confirm a
single probe series returns 200 with real bars (or a genuinely empty answer)
before the full re-pull, then run the normal options pull — settled-clean
windows skip in O(1), only the re-opened rows re-fetch, all under the **8 r/s
AIMD ceiling** (`GROWW_RATE_LIMIT_RPS_CEILING=8.0`,
DEC-GROWW-RATE-CEILING-8-002; soft floor 3 / deep floor 1) **+ the 500 req/min
hard governor — there is NO daily quota** (DEC-GROWW-RATE-PERMIN-001;
CLAUDE.md §2 Rule 6).

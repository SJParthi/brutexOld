# Runbook — Un-settle Option Known-Gap (`SKIPPED`) Ledger Rows

> **Target audience:** the operator (sjparthi) or any Claude session recovering
> from a mass mis-settle of DH-905 known-gap windows in the unified window
> ledger. **Time required:** minutes. **Prerequisites:** the lake root path
> (the directory the dashboard / CLI pulls into) and the project venv.

## When you need this

Per **DEC-OPTION-DH905-CONTINUE-001**, a vendor `DH-905` "no data" answer on an
option fetch window settles that window `SKIPPED` in the unified window ledger
(`<lake_root>/window_ledger/windows.parquet`) and the series continues — so a
fully-dead series drains in ONE paced pass and is never re-fetched.

That settle trusts the vendor's answer. But the DH-905 message **conflates two
causes** ("incorrect parameters **or** no data present" —
`brutex/adapters/groww/errors.py:83-88`), so a *systematic request bug* (wrong
SECURITY_ID mapping, malformed body, a vendor contract change — the
DEC-OPTION-SECURITY-ID-FIX-001 class) can mass-settle windows that the vendor
actually HAS data for. The hostile review of DEC-OPTION-DH905-CONTINUE-001
(finding 2) quantified the blast radius: one such bug can poison every fetched
window of every affected series in a single pull, and every later pull then
reads `clean`/`known_gap_only` with zero vendor calls.

**Detection signals** (any of):

- a pull verdict of `known_gap_only` for series you KNOW traded (e.g. ATM
  NIFTY weeklies);
- `/db` ledger rows with `status == SKIPPED` whose `last_error` carries
  `DH-905` across a suspiciously broad coordinate range;
- `gap_only_series` ≈ the whole fan-out in `universe.pull_options.done`.

The fix is ledger surgery: clear the wrongly-settled rows so the next pull
re-fetches them. **No lake data is at risk** — a known-gap settle writes
ledger state only, never bars.

## 1. Inspect before deleting (Golden Rule #10 — verify, don't assume)

```bash
cd /path/to/bruteX
.venv/bin/python - <<'EOF'
from pathlib import Path
from brutex.data.window_ledger_store import load_window_ledger

LAKE_ROOT = Path("data/lake")  # ← your lake root
ledger = load_window_ledger(LAKE_ROOT)
gaps = [
    r
    for r in ledger.records()
    if r.status == "SKIPPED"
    and r.market == "groww-option"
    and r.last_error is not None
    and "DH-905" in r.last_error
]
print(f"{len(gaps)} option known-gap rows")
for r in gaps[:20]:
    print(r.security_id, r.interval, r.window_start, r.window_end, r.last_error)
EOF
```

The `interval` field carries the full series coordinate
(`OPT|<flag>|<code>|<strike>|<type>` — `option_ingest._coverage_key`), so you
can scope the surgery to exactly the poisoned series.

## 2. Clear the poisoned rows (scoped, atomic)

`WindowLedger` has no public delete (settled rows are normally permanent by
design), so the surgery is a filtered rebuild via the public
`to_rows`/`from_rows` round-trip + the crash-safe `save_window_ledger`
(atomic `*.tmp` + `os.replace`):

```bash
.venv/bin/python - <<'EOF'
from pathlib import Path
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger

LAKE_ROOT = Path("data/lake")  # ← your lake root

# Row tuple layout (window_ledger.WindowLedgerRecord order):
# 0 market · 1 security_id · 2 interval · 3 window_start · 4 window_end ·
# 5 status · 6 bars · 7 attempts · 8 last_error · 9 first_seen_at ·
# 10 last_attempt_at · 11 empty_streak

# ── Scope the surgery: edit this predicate to the poisoned coordinates. ──
def is_poisoned(row: tuple) -> bool:
    market, security_id, interval, status, last_error = (
        row[0], row[1], row[2], row[5], row[8],
    )
    return (
        market == "groww-option"
        and status == "SKIPPED"
        and last_error is not None
        and "DH-905" in str(last_error)
        # Optionally narrow further, e.g.:
        # and interval.startswith("OPT|WEEK|8|")
        # and security_id.startswith("13|")
    )

ledger = load_window_ledger(LAKE_ROOT)
rows = ledger.to_rows()
kept = [r for r in rows if not is_poisoned(r)]
print(f"removing {len(rows) - len(kept)} of {len(rows)} rows")
save_window_ledger(WindowLedger.from_rows(kept), LAKE_ROOT)
EOF
```

## 3. Worst case: reset the whole ledger

If the poisoning is too broad to scope, delete the ledger file itself. This is
safe but expensive: the next pull re-fetches **everything** not seeded back
from the lake (the cold-start `seed_lake_success` bridge re-seeds windows the
Parquet lake already holds bars for, so spot/clean option windows are cheap;
genuinely empty windows re-earn their `EMPTY`/`SKIPPED` status over
re-pulls).

```bash
rm /path/to/lake_root/window_ledger/windows.parquet
```

## 4. Fix the root cause, then re-pull

Clearing rows without fixing the request bug just re-settles them. Confirm a
single probe series returns 200 with real bars (or a genuine DH-905) before
the full re-pull, then run the normal options pull — settled-clean windows
skip in O(1), only the cleared rows re-fetch, all under the `≤ 5 r/s` bucket +
100k/day quota brake (CLAUDE.md §2 Rule 6).

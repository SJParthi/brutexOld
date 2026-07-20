# Expiry-day option-selling tail study — bhavcopy harness

A **reproducible, stdlib-only, single-file** harness
(`bhavcopy_tail_study.py`) that measures the per-expiry-day P&L
distribution of short-premium NIFTY weekly-option variants from public
NSE end-of-day derivatives bhavcopy files — with every stop-loss fill
**bracketed** between:

- **optimistic** (naive): a pierced SL fills AT the trigger, and
- **pessimistic** (the repo's DEC-FILL-WORST-CASE-ONLY-001 decision-bar
  law, MIGRATION.md #1825): a pierced SL fills at the day bar's adverse
  extreme (the HIGH, for a short option being bought back).

The per-day spread between the two brackets is the measured
**naive-vs-honest gap** — the number the tail-study doc needs.

Modeling disclosure: BOTH brackets enter at the day OPEN — the
pessimistic bracket applies the adverse extreme to the **exit only** —
so the pessimistic tail is a **floor** on the true adverse tail
(consistent with honest limit 2 below).

## What it runs

- **Expiry-day discovery** per ISO week, 2019-02-01 → today by default:
  candidate weekday Thursday (Tuesday from 2025-09-01 — see honest
  limits), holiday-shifted by walking BACK up to 3 weekdays, then (if
  still not found) probing up to 2 weekdays FORWARD of the candidate,
  until a bhavcopy actually contains NIFTY options expiring that day.
  Weeks with none found are counted `no_expiry_found`, never
  fabricated; a boundary-week walk-back that lands on the PRIOR week's
  expiry is deduped (`duplicate_expiry_skipped`) — each expiry date is
  processed exactly once.
- **Chain extraction**: the nearest-expiry NIFTY index FUTURE's OPEN is
  the spot proxy (the index itself is not in the derivatives bhavcopy);
  all NIFTY CE/PE rows expiring that day, sanity-screened
  (open ≤ 0 or high < low dropped + counted).
- **Strategy grid** (the honest daily-bar-bracketable subset):
  - V1 short ATM straddle (strike nearest the future open, both legs),
  - V2 short strangle ±1%, V3 short strangle ±2%,
  - per-leg SL grid {20%, 30%, 50%} above entry premium
    (trigger = entry × (1+SL); pierced when HIGH ≥ trigger — boundary
    inclusive), plus a NO-SL hold-to-close baseline,
  - both fill brackets per leg, legs independent,
  - per-lot ₹ columns via the dated NIFTY lot-size table (repo-sourced
    from `brutex/options/lot_size_history.py:58-64`; the pre-2021 span
    is an external-UNVERIFIED back-extension at 75 — flagged in-code).

## Honest limits (read before quoting numbers)

1. **Daily bars.** A daily bar cannot order intraday events. Trailing
   SL, take-profit, entry-time variants, re-entries REQUIRE 1-minute
   data (the Groww pull on the operator box) and are out of scope here.
2. **Traded-price extremes understate the tail.** Bhavcopy OHLC are
   traded prices; a strike that never traded after the open carries
   stale extremes, so the measured pessimistic tail is a LOWER BOUND on
   the true adverse tail.
3. **URL stems + column layouts are UNVERIFIED-until-smoke.** They are
   drawn from public documentation; the build container that produced
   this harness is egress-blocked to the NSE hosts (verified
   2026-07-20), so the parser is fixture-verified only. Run `--smoke`
   once per format era FIRST (below) and eyeball the sample rows.
4. **Costs window.** Gross premium points by default: the repo's honest
   exchange-charge history starts 2024-10-01 (earlier windows are
   explicit refusal rows — DEC-ETC-REGIME-HISTORY-001, MIGRATION.md
   #1524), and the tail conclusion is cost-insensitive (tail losses ≫
   per-trade costs). Expectancy claims must overlay costs on the
   post-2024-10 subwindow: `--costs post2024` applies ₹20/order
   brokerage + sell-side STT (0.10% from 2024-10-01, 0.15% from
   2026-04-01) + NSE ETC 0.03503% for trades ≥ 2024-10-01 and marks
   earlier trades `cost_refused` (rates verified against
   `brutex/costs/constants.py:45,73,77,150`).
5. **SENSEX excluded** pending a BSE bhavcopy probe.
6. **Weekly-expiry regime boundary** (Thursday → Tuesday) is encoded as
   2025-09-01 with an UNVERIFIED-exact-date comment. Self-healing is
   direction-split and honest: the backward walk covers only a
   too-LATE boundary constant (true expiry earlier in the week), and
   the 2-weekday FORWARD probe covers a slightly too-EARLY constant
   (true expiry later in the week); an error beyond both windows is NOT
   self-healed — it surfaces as a `no_expiry_found` cluster in
   `counters.json` (the observability signal to re-derive the
   constant), never a fabricated expiry.

## Run commands (operator box, open egress)

```bash
cd docs/research/expiry_day_option_selling

# 1. MANDATORY first-run probes — one file per format era, eyeball the rows:
python bhavcopy_tail_study.py --smoke 2024-06-06     # old-format era
python bhavcopy_tail_study.py --smoke 2025-03-06     # new UDiFF era

# 2. Full pull + study (downloads ~350 files at ~1 file/sec into ./bhavcopy_cache):
python bhavcopy_tail_study.py --start 2019-02-01 --out-dir results

# 3. Offline re-runs (grid tweaks etc.) — never touches the network:
python bhavcopy_tail_study.py --start 2019-02-01 --out-dir results --from-cache-only

# 4. Net-of-costs overlay on the priced subwindow:
python bhavcopy_tail_study.py --start 2019-02-01 --out-dir results_costs --costs post2024
```

Pull etiquette: ~350 weekly files (plus walk-back probes near
holidays), polite 0.75 s sleep before every request, browser-like UA,
3 retries with backoff, 404s cached as `.404` markers so re-runs are
fully offline. These are public archives — keep it to ~1 file/sec.

## Outputs (`--out-dir`, default `./results`)

| file | contents |
|---|---|
| `per_day_results.csv` | one row per (expiry_date, variant, sl_pct, bracket): entry premia, per-leg trigger hits, exits, P&L points, P&L ₹/lot, spot-proxy open/close/day-move-% (+ cost columns under `--costs`) |
| `summary.md` | tail-first GitHub-markdown tables (below) |
| `counters.json` | honest bookkeeping: weeks scanned, expiry days found, `no_expiry_found` weeks, `duplicate_expiry_skipped`, `fetch_failed` (non-404 failures — day skipped, retried next run), `corrupt_cache` (bad cached zips dropped + re-fetched), dropped rows, variant skips, cost-refused days |

Every summary table carries the footer: *fills bracketed per
DEC-FILL-WORST-CASE-ONLY-001 decision-bar law; daily-bar resolution;
gross of costs (see README)*.

## What this fills in (mapping to the study doc's placeholder tables)

| study-doc placeholder | harness output |
|---|---|
| "Worst N expiry days per variant, naive vs honest fills" | `summary.md` → per-variant **Worst 10 days** tables (optimistic vs pessimistic ₹/lot side-by-side + loss ratio) |
| "P&L distribution percentiles per variant/SL" | `summary.md` → **P&L percentiles** tables (p1/p5/p25/median/p75/p95/p99, ₹/lot) |
| "Win rate / avg win / avg loss / max drawdown" | `summary.md` → **Summary** tables per (SL, bracket) |
| "Naive-vs-honest gap frequency" | `summary.md` → per-SL count of days where the pessimistic loss ≥ 5× the optimistic loss |
| "Year-by-year stability" | `summary.md` → **Per-year totals** subtables |
| "Cost sensitivity (post-2024-10 window)" | `--costs post2024` run → **Net-of-costs totals** section + `per_day_results.csv` net columns |
| raw per-day evidence appendix | `per_day_results.csv` |

## Offline tests (this container / any box, no network)

```bash
python -m pytest docs/research/expiry_day_option_selling/tests_offline.py
```

39 tests: both format parsers over byte-realistic synthetic zips (built
at test runtime — no binary fixtures committed), hand-derived golden
scenarios (quiet day, SL-hit trigger-vs-high fills, 5× gap-day
multiple), holiday walk-back + forward-probe discovery, cross-week
duplicate-expiry dedupe, sanity-screen + unknown-option-side counting,
cost refusal + exact pricing, cache round-trips proving the network
layer is never touched (a raising fetcher is injected), corrupt-cache
self-heal, non-404 fetch-failure resilience, atomic cache writes, and
the https-only guard. The suite deliberately lives OUTSIDE `tests/` and
is NOT wired into ci.yml, so the repo's coverage/mutation gates are
untouched.

## Posture

Standalone research tooling under `docs/research/` (house precedent:
`docs/groww/probe/postclose_settle_probe.py`). Stdlib-only, imports
nothing from `brutex/` (repo values copied with file:line citations),
`ruff check` + `ruff format` + `mypy --strict` clean, no pandas
(repo-wide ban). It never writes into the repo — cache and results land
wherever `--cache-dir`/`--out-dir` point.

# Day 1 Report — Phase 0.5 Dopamine Sprint

**Date:** 2026-05-16
**Branch:** `claude/day-1-dopamine-sprint-VDDS1`
**Phase:** 0.5 — Dopamine Day
**Status:** 🔄 IN PROGRESS

## What landed today

### Hour 0-2: NSE/BSE Trading Calendar ✅

`brutex/calendars/nse.py` — 280 lines, fully tested.

**What it does:**
- NSE/BSE holiday list 2021-2026 (90 verified holidays across 6 years)
- Diwali Muhurat sessions (one-hour evening sessions per year)
- Trading session detection: regular (09:15-15:30 IST) / muhurat / half-day / holiday
- Pre-open auction window detection (09:00-09:15 → EXCLUDE)
- Forced 15:20 IST exit rule
- Tick size locked: `5 paisa` (₹0.05) throughout 2021-2026
- Helper: `trading_days_between(start, end) → list[date]`
- Helper: `expected_bars_per_day_for_tf(tf_minutes) → int`

**Tests:** 35/35 passing in 0.09 seconds.

| Test class | Count | Status |
|---|---|---|
| Holidays (Republic Day, Independence Day, Diwali, etc.) | 5 | ✅ |
| Weekends (Sat / Sun / Monday boundary) | 3 | ✅ |
| Trading day filter | 3 | ✅ |
| Session lookup (regular / muhurat / holiday / weekend) | 4 | ✅ |
| Trading-days-between range | 3 | ✅ |
| Expected bars per TF (1m, 5m, 15m, 30m, 60m, daily) | 9 | ✅ |
| Forced 15:20 exit rule | 3 | ✅ |
| Pre-open window | 4 | ✅ |
| Constants (tick size) | 1 | ✅ |

### Hour 2-4 (batch 2): Static Daily Fibonacci ✅

`brutex/indicators/fib_levels.py` — 12-level Fib indicator anchored
to Previous Day's High and Low, computed once per trading day and
held constant intraday across all 21 timeframes.

**Verified against operator's AmiBroker v11 screenshot:**
- BANKNIFTY 13-May-2026: PDH=54,000, PDL=53,594.40
- Expected 23% level = 53,904.28
- Computed 23% level = 53,904.28 ✓ **bit-exact match**

**The 12 levels (all in int64 paisa):**

| Level | Formula | Type |
|---|---|---|
| 0% | PDH | anchor (top) |
| 23% | PDH − 0.236×range | retracement |
| 38% | PDH − 0.382×range | retracement |
| 50% | (PDH+PDL)/2 | midpoint |
| 62% | PDH − 0.618×range | retracement |
| 78% | PDH − 0.786×range | retracement |
| 100% | PDL | anchor (bottom) |
| 127% | PDL − 0.272×range | extension below |
| 162% | PDL − 0.618×range | extension below |
| 200% | PDL − 1.000×range | extension below |
| 262% | PDL − 1.618×range | extension below |
| 424% | PDL − 3.236×range | extension below |

**Tests:** 17/17 passing in 40 ms. Includes exact-match oracle test
against operator's AmiBroker screenshot.

### Day 1 commit so far (combined)

| Module | Lines | Tests | Status |
|---|---|---|---|
| `brutex/calendars/nse.py` | 280 | 35 ✅ | Done |
| `brutex/indicators/fib_levels.py` | 175 | 17 ✅ | Done |
| Placeholder skeletons (`backtest`, `metrics`, `ranking`, `sprint`, `visualization`, `adapters/groww`) | docstring stubs | — | Ready to fill |
| **Total tests passing** | **52** | | **in 130 ms** |

### What's still pending (Hours 4-20)

| Hour | Module | Status |
|---|---|---|
| 4-6 | `brutex/indicators/pivots.py` (Standard Pivots P, R1-R4, S1-S4) | ⬜ next |
| 4-6 | `brutex/indicators/ema.py` (EMA 20, EMA 200) | ⬜ |
| 4-6 | `brutex/data/resampler.py` (1m → 21 TFs) | ⬜ |
| 6-10 | `brutex/indicators/conditions.py` (52 vocab atoms) | ⬜ |
| 4-6 | `brutex/adapters/groww/` (HTTP client + token + auto-renew) | ⏸️ pending Groww token |
| 4-6 | Real 5y Groww data fetched + Parquet stored | ⏸️ pending Groww token |
| 10-14 | `brutex/backtest/engine.py` (Polars vectorized) | ⬜ |
| 14-16 | Sweep run (~12.6M trials, ~30-60 min wall) | ⏸️ pending Groww data |
| 16-18 | `brutex/metrics/calculator.py` (30+ metrics) | ⬜ |
| 18-20 | `brutex/ranking/composite.py` + PNG charts | ⬜ |

## What needs operator action

1. ⬜ **Paste Groww access token** into `~/.brutex/groww/access_token.json` (~1 min)
2. ⬜ **Plug in 8 TB external SSD** at `/Volumes/QuantData` (~30 sec)
3. ⬜ (Optional) Confirm aiogram 3.x for Telegram (vs python-telegram-bot)
4. ⬜ (Optional) Confirm FastAPI + Jinja2 + HTMX for Day 1 dashboard
5. ⬜ (Optional) Approve sudoers entry for `/usr/bin/powermetrics`

**Without #1 and #2 the sprint cannot fetch real data.** Skeleton work (#1 calendar done; resampler/indicators/backtest engine to follow on synthetic data) continues regardless.

## Dopamine of the day

**Calendar + Fib indicator both locked.** 52 tests passing in 130 ms.

Bit-exact match against operator's AmiBroker v11 screenshot
(BANKNIFTY 13-May-2026 23% level = 53,904.28). Proof the formula
is correct — no hallucination, traceable to a verified screenshot.

**First strategy template** indicator set:
| # | Indicator | Status |
|---|---|---|
| 1 | Static Daily Fib (12 levels) | ✅ DONE |
| 2 | Standard Pivots (P, R1-R4, S1-S4) | ⬜ next |
| 3 | EMA 20 | ⬜ next |
| 4 | EMA 200 | ⬜ next |
| 5 | Previous Day OHLC | ⬜ next |
| 6 | Day High / Day Low (running) | ⬜ next |

## Next commit target

- `brutex/indicators/pivots.py` — Standard Pivots (P, R1-R4, S1-S4)
- `brutex/indicators/ema.py` — EMA 20, EMA 200
- Spec amendment: vocab decision #11 says R1-R3 / S1-S3 — bump to R1-R4 / S1-S4 to match TradingView Traditional Auto pivot levels operator uses

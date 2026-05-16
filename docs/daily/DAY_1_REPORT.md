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

### What's still pending (Hours 2-20)

| Hour | Module | Status |
|---|---|---|
| 2-4 | `brutex/adapters/dhan/` (HTTP client + token + auto-renew) | ⏸️ pending Dhan token |
| 2-4 | `brutex/data/resampler.py` (1m → 21 TFs) | ⬜ next |
| 4-6 | Real 5y Dhan data fetched + Parquet stored | ⏸️ pending Dhan token |
| 6-10 | `brutex/indicators/conditions.py` (52 vocab atoms) | ⬜ |
| 10-14 | `brutex/backtest/engine.py` (Polars vectorized) | ⬜ |
| 14-16 | Sweep run (~12.6M trials, ~30-60 min wall) | ⏸️ pending Dhan data |
| 16-18 | `brutex/metrics/calculator.py` (30+ metrics) | ⬜ |
| 18-20 | `brutex/ranking/composite.py` + PNG charts | ⬜ |

## What needs operator action

1. ⬜ **Paste Dhan access token** into `~/.brutex/dhan/access_token.json` (~1 min)
2. ⬜ **Plug in 8 TB external SSD** at `/Volumes/QuantData` (~30 sec)
3. ⬜ (Optional) Confirm aiogram 3.x for Telegram (vs python-telegram-bot)
4. ⬜ (Optional) Confirm FastAPI + Jinja2 + HTMX for Day 1 dashboard
5. ⬜ (Optional) Approve sudoers entry for `/usr/bin/powermetrics`

**Without #1 and #2 the sprint cannot fetch real data.** Skeleton work (#1 calendar done; resampler/indicators/backtest engine to follow on synthetic data) continues regardless.

## Dopamine of the day

Calendar module locked. NSE 2021-2026 holidays + Muhurat sessions + forced 15:20 exit + tick size 5 paisa = ready. 35 tests passing in 90 ms. **First brick of the building is in.**

## Next commit target

- Resampler: `brutex/data/resampler.py` (1m → 21 TFs, Polars vectorized)
- 21 TF list verified against spec §Workload Math
- Unit tests on synthetic 1m data

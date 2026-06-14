# Runbook — Groww live-feed → 1-minute candle → backtest parity

> **What this answers:** is our LIVE feed's 1-minute candle identical to the
> historical 1-minute candle we backtest on? Plus: how many websocket
> connections one key holds, and whether ticks arrive in **milliseconds or
> seconds**.
>
> **Safety (non-negotiable):** READ-ONLY market data. **No order placement
> anywhere.** The token is read from the environment only — never hardcoded,
> never printed, never committed (`.env` stays gitignored).
> Decision: `DEC-GROWW-LIVE-PARITY-001` (REFERENCE/RESEARCH only — NOT the
> brute-force engine).

Runs on the operator's Mac (real `growwapi` + TOTP + live market). The
websocket itself cannot run in CI — but every candle/parity/cadence computation
is 100%-tested offline.

---

## 0. One-time setup

```bash
pip install -e ".[groww,data]"          # the growwapi SDK + lake deps
export BRUTEX_GROWW_API_KEY=...          # never hardcode / commit
export BRUTEX_GROWW_TOTP_SECRET=...
```

## 1. (Optional, tonight) — find the websocket connection ceiling

```bash
python -m brutex.live probe-connections --max 100
# logs: max_held, failed_at, the exact refusal error of connection #N+1.
```

## 2. Monday at/just-before 09:15 IST — capture the session

```bash
mkdir -p runs
python -m brutex.live capture \
  --out runs/live_2026-06-15.jsonl \
  --seconds 23400 \
  --feed index            # subscribe_index_value for NIFTY/BANKNIFTY/SENSEX (+ VIX)
# 23400s ≈ 6h30m (a 09:00→15:30 cushion). Ctrl-C any time — the JSONL log is
# flushed per tick, so it is always durable + resume-safe (re-run with --resume).
```

The default `--symbols` is `NSE-NIFTY,NSE-BANKNIFTY,BSE-SENSEX,NSE-INDIAVIX`
(the backtest universe = the parity target). Override with `--symbols` to add
more, or `--feed ltp` for cash equities.

## 3. After the close — tick cadence (ms vs seconds)

```bash
python -m brutex.live cadence --in runs/live_2026-06-15.jsonl
# logs: samples, median/min/max inter-arrival ms, and a verdict:
#   tick-by-tick (<100ms) / sub-second / ≈1-per-sec snapshot / multi-second.
```

## 4. Build the LIVE 1-minute candles (09:15:00–15:29:00 IST)

```bash
python -m brutex.live build \
  --in runs/live_2026-06-15.jsonl \
  --out runs/live_candles.json \
  --date 2026-06-15
# 375 bars per instrument max; only minutes that actually ticked produce a bar
# (a missing live minute is itself a parity signal — gaps are NOT synthesised).
```

## 5. Export the BACKTEST 1-minute candles for the SAME day

Produce `runs/bt_candles.json` in the same shape —
`{ "<instrument_key>": [ {timestamp, open, high, low, close, volume,
open_interest}, ... ] }` — from the lake's `1minute` candles (the dashboard /
CLI 1-minute pull). The `instrument_key` strings must match the live keys so the
parity aligns by instrument (rename either side's keys if they differ).

## 6. Parity — the EOD verdict

```bash
python -m brutex.live parity \
  --live runs/live_candles.json \
  --backtest runs/bt_candles.json \
  --out runs/parity.md
# exit 0 = IDENTICAL, exit 1 = divergence. Open runs/parity.md: one verdict row
# per instrument + a divergence table (minute IST · field · live · backtest).
```

`runs/parity.md` is the deliverable — a human-readable table that says
**IDENTICAL ✅** or shows exactly which minute/field diverged.

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

---

# Part B — Dashboard one-click go-live (the N-connection path)

> Sections 0–6 above are the **CLI / parity** deep-dive. This part is the
> **dashboard toggle** flow that wires the whole universe — all **775 spot**
> (25 admitted indices + 750 stocks) **+ the F&O legs** — across **N
> websocket connections** from a single ON switch. It is the operator path
> shipped by `DEC-LIVE-DASHBOARD-WIRE-001` (MIGRATION.md #780), building on
> `DEC-LIVE-MULTICONN-MANIFEST-001` (#779), `DEC-LIVE-DUAL-FEED-001` (#774),
> and `DEC-LIVE-CREDS-BRIDGE-001` (#771). Read-only market data, no orders
> (CLAUDE.md §3).

## B0. Save the creds ONCE (no env export needed)

The dashboard **Credentials** page saves the Groww API key + TOTP secret to
`~/.brutex/credentials.json` (mode `0600`). The Live toggle injects them into
the capture subprocess's **environment** — never on the command line, so `ps`
stays clean (`DEC-LIVE-CREDS-BRIDGE-001`, #771). Save once → "Live ON" just
works; no `export` step.

> ⚠️ **Rotate first.** If a key/secret was ever pasted into a chat, an email,
> or a screenshot, treat it as compromised — rotate it in the Groww account and
> save the NEW pair on the Credentials page before going live
> (`DEC-LIVE-CREDS-BRIDGE-001`; the TOTP creds-flow facts are CLAUDE.md §6).

## B1. Tonight (pre-market) — the DRY-RUN that proves the plumbing

The market is closed outside **09:15–15:30 IST** (CLAUDE.md §7), so this run
proves everything EXCEPT the ticks — which is exactly what you want to de-risk
ahead of the open.

1. Open the dashboard → **Live** tab → flip the toggle **ON**.
2. The toggle writes the subscription **manifest** from the already-persisted
   tables — the `instruments` table (the 775) + the F&O `contracts` registry
   (the legs) — then spawns `capture-all --manifest … --report … --feed both`
   (`DEC-LIVE-DASHBOARD-WIRE-001`, #780).
3. Watch the **per-connection proof** (the `#connGrid` table, polled from
   `GET /api/v1/live/connections`):

   ```
   #0  feed: both   775   ← 25 indices on the index-value feed + 750 stocks on ltp
   #1  feed: ltp   1000   ← F&O legs
   #2  feed: ltp   1000
   …                       (one extra connection per 1000 legs; total = 1 + ceil(legs/1000))
   ```

**Healthy dry-run = ✅ connections open · ✅ proof table populated · ✅ status
`running` · ✅ heartbeat ticking · ⬜ ticks = 0.** Zero ticks is CORRECT with
the market closed — it is not a failure.

**If something OTHER than zero ticks looks wrong**, that is the real signal to
catch tonight:

| Symptom | Likely cause | Action |
|---|---|---|
| Status flips to `stopped` / auth error in the log | creds missing / not rotated / wrong TOTP | re-save on the Credentials page (B0) |
| `legs: []`, only `#0` shows | F&O `contracts` registry not pulled yet | run "Fetch all expiries/contracts" on `/universe` |
| A connection refuses to open at #N | per-key websocket ceiling | `python -m brutex.live probe-connections --max 100` (§1) and cap below #N |

## B2. Monday 09:15 IST — the real run

With the dry-run green, the open adds only ONE new variable: the ticks.

1. Leave Live **ON** (or flip it on a minute before 09:15). The
   restart-watchdog auto-relaunches a dropped capture with bounded backoff
   (`DEC-LIVE-DASHBOARD-WIRE-001`, #780), so a transient feed blip self-heals.
2. At **09:15:00 IST** the indices start ticking (CLAUDE.md §7); the
   per-connection proof's tick counters move and the live lake fills.
3. Spot ticks become **live 1-minute candles** (09:15:00–15:29:00, 375 bars
   max/instrument — same builder as §4; a missing live minute is left as a gap,
   never synthesised).

The adapter paces every connection with the adaptive token bucket + the
500 req/min governor (CLAUDE.md §6, `DEC-GROWW-RATE-PERMIN-001`); the per-second
ceiling is the operator-directed 8 r/s — do not raise either without approval.

## B3. End of day — the parity verdict

Run §4 → §5 → §6 against Monday's capture. `runs/parity.md` is the proof the
**live** 1-minute candle equals the **backtest** 1-minute candle, instrument by
instrument. **IDENTICAL ✅** is the go-live success criterion.

## B4. What is proven offline vs only at the open

Market-hours facts below are CLAUDE.md §7; the feed/universe facts are
CLAUDE.md §6.

| Layer | Proven without the market? | Where |
|---|---|---|
| Dashboard view · N-connection plan · per-connection proof · the 775/legs manifest | ✅ yes — deterministic from the stored tables, 100% tested | B1 · `DEC-LIVE-DASHBOARD-WIRE-001` #780 |
| Sockets open + Groww accepts the subscribe calls | 🟡 dry-run can confirm (0 ticks) — real-socket path is the operator-Mac run | B1 · CLAUDE.md §6 |
| Real ticks → 1-minute candles → parity | ❌ needs the market open (09:15 IST) | B2–B3 · CLAUDE.md §7 |

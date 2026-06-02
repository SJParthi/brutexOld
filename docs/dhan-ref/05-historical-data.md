# Dhan v2 — Historical & Intraday Data

> **Verified source:** DhanHQ v2 Historical Data docs +
> `tickvault/docs/dhan-ref/05-historical-data.md` (fetched 2026-05-31).
> This is the **core Track 1 ingest surface**.

> ## SCOPE LOCK — SPOT UNDERLYING ONLY (operator 2026-05-31)
>
> These two endpoints (`/charts/historical`, `/charts/intraday`) are
> used **ONLY for underlying SPOT** — the 3 brute-force indexes
> (NIFTY 50 / BANKNIFTY / SENSEX), INDIA VIX (reference), and the 200
> F&O-eligible single-stock **spot** rows. **Options are NEVER fetched
> from these endpoints.** In a backtest every option contract is
> already expired, so **ALL option OHLC is pulled exclusively from the
> expired-options rolling endpoint** (`/charts/rollingoption`, see
> `06-expired-options-data.md`). Consequently the `expiryCode` / `oi`
> derivative fields below are **NOT used by Track 1** — they are
> documented for completeness only.

## Endpoints

| Purpose | Method + path | Date format |
|---|---|---|
| Daily OHLC | `POST https://api.dhan.co/v2/charts/historical` | `YYYY-MM-DD` (toDate non-inclusive) |
| Intraday OHLC | `POST https://api.dhan.co/v2/charts/intraday` | `YYYY-MM-DD HH:MM:SS` |

## Request body

Common required fields (both endpoints):

| Field | Type | Notes |
|---|---|---|
| `securityId` | string | from instrument master — **never hardcode** |
| `exchangeSegment` | enum | `NSE_EQ`, `NSE_FNO`, `BSE_EQ`, … |
| `instrument` | enum | `EQUITY`, `INDEX`, `OPTIDX`, … |
| `fromDate` | string | start of range |
| `toDate` | string | end of range (**non-inclusive**) |

Intraday-only:

| Field | Type | Notes |
|---|---|---|
| `interval` | string | one of `"1"`, `"5"`, `"15"`, `"25"`, `"60"` minutes |

Optional (**NOT used by Track 1 — spot-only ingest**, see Scope Lock above):

| Field | Type | Notes |
|---|---|---|
| `expiryCode` | int | `0`=current, `1`=next, `2`=far (derivatives) |
| `oi` | bool | request open-interest series |

## Response shape — columnar parallel arrays

```json
{
  "open":          [ ... ],
  "high":          [ ... ],
  "low":           [ ... ],
  "close":         [ ... ],
  "volume":        [ ... ],
  "timestamp":     [ ... ],
  "open_interest": [ ... ]
}
```

All arrays are index-aligned: `open[i] … timestamp[i]` is one candle.
This **differs from Groww's row-of-arrays** (`[ts, o, h, l, c, v, oi]`
per candle) — the Dhan parser transposes columns → `CandleBar` rows.

## Units & time

- **Prices are RUPEES (float).** The parser keeps them as exact `Decimal`
  rupees (NO `× 100`); int64 paisa is the cost/display unit only, never
  price storage (DEC-PAISA-DOC-RECONCILE-001; CLAUDE.md §4 / §6).
- **Timestamps are UNIX epoch UTC seconds.** Convert to IST
  (`+5:30`) only at the display boundary; store UTC. Daily bars stamp
  IST-midnight (`18:30` UTC of the prior day).

## Constraints

| Constraint | Value |
|---|---|
| Intraday history depth | last **5 years** |
| Intraday max window per call | **90 days** |
| Daily history depth | back to instrument inception |
| Supported intraday intervals | 1, 5, 15, 25, 60 min |
| Rate limit | 5 req/s, 100,000/day |

## BruteX Track 1 mapping (CLAUDE.md §1 + DEC-CANONICAL-1M-SOURCE-001)

- Track 1 fetches **`interval="1"` (1-minute) ONLY** + daily
  (`/charts/historical`) for PDH/PDL anchors. The 19 derived intraday
  timeframes are **deterministically resampled from the 1m base**
  (`brutex/feed/resampler.py`) — Dhan's native 5/15/25/60 aggregations
  are NOT ingested.
- Chunk window for 1m = **90 days/call** (one `chunks.py` constant,
  replacing Groww's 30/90/180 tiering — Dhan has a single 90-day cap).
- **Unlike Groww**, Dhan reconstructs each minute from the official
  exchange feed (operator domain note, DEC-CANONICAL-1M-SOURCE-001) —
  this revert's data-fidelity motivation.

## Honest-envelope flags

- Index `securityId`s (NIFTY 50 / BANKNIFTY / SENSEX / INDIA VIX) are
  **not in the docs** — Phase 0 must resolve them from the instrument
  master and a live probe before any production pull.
- The `90-day` intraday cap means the rolling `2021-01-01 → today`
  range needs ~21 chunks/instrument for 1m (vs Groww's 30-day → ~64),
  re-tuning the chunk planner.

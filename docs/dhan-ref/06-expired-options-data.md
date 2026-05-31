# Dhan v2 — Expired Options Data (Historical Rolling)

> **Verified source:** DhanHQ v2 Expired Options Data docs
> (https://dhanhq.co/docs/v2/expired-options-data/, pasted by operator
> 2026-05-31). Relevant to the **Track 2 / lake options** surface
> (DEC-P0-063 stock + index options); the **brute-force engine is
> unchanged** (CLAUDE.md §1/§2 Rule 3).

## Endpoint

```
POST https://api.dhan.co/v2/charts/rollingoption
```

Returns expired options contract data on a **rolling basis** — last
**5 years**, strike-wise, relative to spot (ATM, ATM±1, …). Stored at
**minute level**. Up to **30 days per call**. Covers **both index and
stock options**.

> ## CANONICAL OPTIONS SOURCE (operator 2026-05-31)
>
> This is the **ONLY** endpoint BruteX uses for option OHLC. In a
> backtest every option contract is already expired, so option data is
> **ALWAYS** pulled from `/charts/rollingoption` — never from
> `/charts/intraday` or `/charts/historical` (those are **spot
> underlying only**, see `05-historical-data.md`). Spot ⇒
> intraday/historical; options ⇒ rollingoption. No exceptions.
>
> **1-minute only (DEC-DHAN-1M-ONLY-001, MIGRATION #343):** Track 1
> pulls options at `interval = "1"` (1-minute) **only**; the 5/15/25/60
> values are never requested. Higher timeframes are resampled from the
> 1m base, never fetched.

## Request body

| Field | Type | Description |
|---|---|---|
| `exchangeSegment` | enum string | e.g. `NSE_FNO` |
| `interval` | enum int | `1, 5, 15, 25, 60` minute |
| `securityId` | string | underlying exchange id |
| `instrument` | enum string | e.g. `OPTIDX` |
| `expiryFlag` | enum string | `WEEK` or `MONTH` |
| `expiryCode` | enum int | expiry selector |
| `strike` | enum string | `ATM`; up to `ATM±10` (index near-expiry), `ATM±3` (others) |
| `drvOptionType` | enum string | `CALL` or `PUT` |
| `requiredData` | array | any of `open high low close iv volume strike oi spot` |
| `fromDate` | string | start of range |
| `toDate` | string | end of range (**non-inclusive**) |

## Response shape

```json
{
  "data": {
    "ce": {
      "iv": [], "oi": [], "strike": [], "spot": [],
      "open": [354, 360.3], "high": [], "low": [], "close": [],
      "volume": [], "timestamp": [1756698300, 1756699200]
    },
    "pe": null
  }
}
```

Per-side (`ce` / `pe`) columnar parallel arrays — same transpose +
`× 100` (rupees→paisa) + UTC-epoch handling as `05-historical-data.md`.
A null side (`"pe": null`) means no data for that leg/window.

## Honest-envelope flags

- This endpoint is **NOT part of the Track 1 brute-force engine**.
  It feeds the lake (DEC-P0-063 options expansion) for future use.
- `strike` is relative-to-spot (ATM±N), not an absolute strike — the
  parser must record the resolved `strike[]` array per bar.

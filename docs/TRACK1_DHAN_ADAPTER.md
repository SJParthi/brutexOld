# Track 1 — Dhan Adapter Specification

> **Decision:** DEC-GROWW-DHAN-REVERT-001 (MIGRATION.md #341,
> ADR `docs/adr/0002-groww-to-dhan-revert.md`). Verified vendor facts
> live in `docs/dhan-ref/` (the single source of truth — Golden
> Rule #1/#2). This spec maps those facts onto the market-neutral
> `MarketAdapter` contract in `brutex/adapters/protocol.py`.
>
> **Status:** spec (installment 1). The `brutex/adapters/dhan/` code
> is a follow-on installment; the full 4-agent hostile review
> (CLAUDE.md §9) gates that code, not this spec.

## 1. Scope

Track 1 = **signal-only backtest research** on the 3 brute-force spot
indexes (NIFTY 50 / BANKNIFTY / SENSEX) + INDIA VIX (reference) + the
200 F&O-eligible single-stock **spot** rows for the lake. Only Dhan's
**Data APIs** are in scope; Orders / Funds / Live Feed / Postback /
EDIS / Full Market Depth are OUT (CLAUDE.md §1).

## 2. Endpoint → responsibility map

| BruteX need | Dhan surface | Notes |
|---|---|---|
| Spot intraday OHLC | `POST /v2/charts/intraday` | interval `"1"` (1-minute) **only** (DEC-DHAN-1M-ONLY-001), 90-day window |
| Spot daily OHLC (PDH/PDL) | `POST /v2/charts/historical` | **1-day** only, to inception |
| Expired option OHLC | `POST /v2/charts/rollingoption` | ALL options, interval `"1"` (1-minute) **only** (DEC-DHAN-1M-ONLY-001); every contract is expired in a backtest |

**1-minute-only fetch set (DEC-DHAN-1M-ONLY-001, #343):** spot pulls
1m intraday + 1d daily; options pull 1m only. Dhan's native 5/15/25/60
intervals are **never fetched** — all 19 derived intraday timeframes are
resampled from the 1m base (`brutex/feed/resampler.py`).
| Instrument universe | scrip-master CSV (`images.dhan.co`) | `SECURITY_ID` numeric key |

**Spot/options split (locked):** the two `charts/*` historical
endpoints serve **spot underlyings only**; option OHLC is **always**
pulled from `charts/rollingoption`. See `docs/dhan-ref/05-…` + `06-…`.

## 3. `MarketAdapter` implementation plan

The protocol asks three questions (`brutex/adapters/protocol.py`):

1. **`list_instruments`** → parse the detailed scrip-master CSV into
   normalised `brutex.models.Instrument` rows. The universe (3 indexes
   + VIX + 200 stocks) returns in pages; `SECURITY_ID` is the key.
2. **`list_derivatives`** → for the lake options surface, enumerate
   option contracts per underlying via the scrip master; spot-only
   Track 1 runs return empty (skips the expiry/contract dance).
3. **`fetch_ohlcv`** → POST the relevant `charts/*` endpoint, transpose
   the columnar response, and emit `CandleBar` rows.

## 4. Parser contract (the int64-paisa boundary)

Dhan returns **columnar parallel arrays** (vs Groww's row-arrays):

```
{ open:[], high:[], low:[], close:[], volume:[], timestamp:[], open_interest:[] }
```

The parser MUST, per `docs/dhan-ref/05-historical-data.md`:

- **Transpose** column arrays → one `CandleBar` per index `i`.
- **× 100** every price (Dhan prices are **rupees**) to preserve
  `int64` paisa (CLAUDE.md §4 money rule). Rounding only at display.
- Treat `timestamp` as **UNIX epoch UTC seconds**; store UTC, convert
  to IST at the display boundary only.
- Tolerate a null/short side (`rollingoption` `"pe": null`) and
  null-volume index bars (mirrors the existing empty-bar resilience).

## 5. Chunk planner

`docs/dhan-ref/05-…`: intraday is capped at **90 days/call**, history
**5 years**. The 1m-only pull (DEC-CANONICAL-1M-SOURCE-001, #339) over
the rolling `2021-01-01 → today` range needs ~21 chunks/instrument
(vs Groww's 30-day → ~64). `rollingoption` caps at **30 days/call**.
This replaces Groww's 30/90/180 tiering with a single 90-day spot cap
+ a 30-day options cap.

## 6. Auth + rate limiter

- **Auth** (`docs/dhan-ref/02-…`): header `access-token` (JWT, 24h) +
  `dhanClientId`, refreshed unattended via TOTP at
  `auth.dhan.co/app/generateAccessToken` — satisfies §11 AUTOMATED
  ("zero clicks/day"). Secrets stored mode-0600 under `~/.brutex/dhan/`.
- **Rate limit** (`docs/dhan-ref/01-…`): Data API **5 r/s · 100k/day**.
  The existing adaptive token bucket re-pins to a 3 r/s baseline
  (`5 × 0.6` safety) auto-tuning within `[3..5]`; `quota_tracker.py`
  enforces the 100k/day budget.

## 7. Instrument / index resolution — Phase 0 HARD-GATE

Per `docs/dhan-ref/09-instrument-master.md`, index `SECURITY_ID`s and
INDIA VIX presence are **NOT in the docs** and MUST be resolved from
the detailed CSV + confirmed against the historical endpoint **before
any production pull**. `SECURITY_ID`s are stored in the universe
catalog, **never hardcoded** (the Dhan HARD rule).

## 8. Migration mapping (Groww → Dhan)

| Surface | Groww (remove) | Dhan (build) |
|---|---|---|
| Adapter pkg | `brutex/adapters/groww/` | `brutex/adapters/dhan/` |
| Vendor dep | `growwapi` extra | plain authenticated REST (no SDK) |
| Instrument key | `groww_symbol` (string) | `SECURITY_ID` (numeric) |
| Watermark field | `groww_symbol` | `security_id` |
| `Broker` literal | `groww` | `dhan` |
| Audit `vendor` | `groww` | `dhan` |

Unchanged (vendor-neutral, §11 COMMON): data lake, feed/resampler,
exec sim, `brutex.money`, audit hash-chain, adaptive token bucket,
work-item state machine, `adapters/protocol.py`. The brute-force
engine is untouched (CLAUDE.md §2 Rule 3).

## 9. Honest envelope (Golden Rule #10)

- This is a **spec**, not code — `brutex/adapters/dhan/` does not exist
  yet; the strip + rebuild land as individually-green follow-on commits.
- Several facts (index ids, INDIA VIX presence, Data-bucket sharing)
  are UNVERIFIED pending the Phase-0 probe and are flagged as such in
  `docs/dhan-ref/*`, not asserted here.

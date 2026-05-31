# Dhan v2 — Instrument Master (Scrip Master)

> **Verified source:** DhanHQ v2 Instrument List docs +
> `tickvault/docs/dhan-ref/09-instrument-master.md` (fetched 2026-05-31).

## CSV URLs

| File | URL | Use |
|---|---|---|
| Compact | `https://images.dhan.co/api-data/api-scrip-master.csv` | minimal columns |
| **Detailed** | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` | **required for F&O** (underlying ids, ISIN, lot/tick) |
| Per-segment | `https://api.dhan.co/v2/instrument/{exchangeSegment}` | API form |

Download **fresh daily** — listings, expiries, and corporate actions
change the file.

## Detailed CSV column schema (order)

**VERIFIED complete column set** (extracted from the operator's actual
`api-scrip-master-detailed` 2026-05-31 — 31 columns, nothing dropped):

```
EXCH_ID, SEGMENT, SECURITY_ID, ISIN, INSTRUMENT, UNDERLYING_SECURITY_ID,
UNDERLYING_SYMBOL, SYMBOL_NAME, DISPLAY_NAME, INSTRUMENT_TYPE, SERIES,
LOT_SIZE, SM_EXPIRY_DATE, STRIKE_PRICE, OPTION_TYPE, TICK_SIZE, EXPIRY_FLAG,
BRACKET_FLAG, COVER_FLAG, ASM_GSM_FLAG, ASM_GSM_CATEGORY, BUY_SELL_INDICATOR,
BUY_CO_MIN_MARGIN_PER, BUY_CO_SL_RANGE_MAX_PERC, BUY_CO_SL_RANGE_MIN_PERC,
BUY_BO_MIN_MARGIN_PER, BUY_BO_PROFIT_RANGE_MAX_PERC, BUY_BO_PROFIT_RANGE_MIN_PERC,
MTF_LEVERAGE, SM_UPPER_LIMIT, SM_LOWER_LIMIT, SM_FREEZE_QTY
```

| Field | Meaning |
|---|---|
| `EXCH_ID` | `NSE`, `BSE`, `MCX` |
| `SEGMENT` | segment code `C, D, E, I, M` |
| `SECURITY_ID` | **numeric primary key for every API call** |
| `UNDERLYING_SECURITY_ID` / `UNDERLYING_SYMBOL` | derivative → underlying link |
| `OPTION_TYPE` | `CE` / `PE` |
| `EXPIRY_FLAG` | `M` (monthly) / `W` (weekly) |
| `LOT_SIZE`, `TICK_SIZE`, `STRIKE_PRICE`, `SM_EXPIRY_DATE` | contract economics |
| **`SM_FREEZE_QTY`** | **freeze quantity** (max single-order qty) |
| `SM_UPPER_LIMIT` / `SM_LOWER_LIMIT` | price bands (DPR) |
| `MTF_LEVERAGE` | MTF multiplier (equity) |
| `ASM_GSM_FLAG` / `ASM_GSM_CATEGORY` | surveillance status |
| `BRACKET_FLAG` / `COVER_FLAG` / `BUY_SELL_INDICATOR` | order-type permissions |
| `BUY_CO_*` / `BUY_BO_*` | cover/bracket-order margin + range params |

> **Nothing missed:** the `instruments` catalog table persists the **full
> 31-column row**; the vendor-neutral `brutex.models.Instrument` carries the
> engine-relevant subset (identity, classification, **lot/tick/freeze**,
> expiry, strike, underlying). Parser: `brutex/adapters/dhan/instruments.py`.

## Tables (storage layout)

- **`instruments`** (DuckDB catalog) — one row per universe underlying
  (lean-ingest per DEC-INSTRUMENT-UNIVERSE-FAST-001), keyed by
  `SECURITY_ID` (BIGINT PK → O(1)); full 31-col fidelity + provenance
  (`vendor='dhan'`, `snapshot_date`, `csv_sha256`).
- **spot bars** (Parquet) — `bars/<exch>/<seg>/<security_id>/<interval>/<yyyy>/<mm>.parquet`,
  schema `timestamp,open,high,low,close,volume,open_interest`; 1m + 1d.
- **expired-options bars** (Parquet) — `options/<underlying_security_id>/<expiry_flag>/<expiry_code>/<strike_rel>/<CE|PE>/<interval>/<yyyy>/<mm>.parquet`;
  adds `iv,oi,strike,spot`; 1m only.

## SecurityId rule (HARD)

> "The instrument master CSV is the sole source of truth for SecurityId
> values. **Never hardcode SecurityIds.**"

Lookup flow: **Symbol / ISIN → CSV row → SECURITY_ID**. This replaces
Groww's `groww_symbol` string keying with Dhan's numeric `SECURITY_ID`
throughout the watermark schema, work-item store, and dashboard
(re-pinned in the adapter-flip commit, DEC-GROWW-DHAN-REVERT-001).

## ExchangeSegment enums (Data-API scope)

`NSE_EQ`, `NSE_FNO`, `BSE_EQ` (+ others in the Dhan annexure). Track 1
brute-force universe → `NSE_EQ` (NIFTY/BANKNIFTY indices via `INDEX`
instrument), `BSE_EQ` (SENSEX), and `NSE_FNO` for the lake options.

## Track 1 index resolution (Phase 0 GATE)

The docs do **NOT** list `SECURITY_ID` for NIFTY 50 / BANKNIFTY /
SENSEX / INDIA VIX. Phase 0 MUST:

1. Download the detailed CSV.
2. Filter `INSTRUMENT == INDEX` (or exchange index rows) and match
   `SYMBOL_NAME` / `DISPLAY_NAME` for the four indices.
3. Record the resolved `SECURITY_ID`s in the universe catalog (stored,
   not hardcoded) and confirm each returns candles from the historical
   endpoint.

Until that probe passes, **no production pull** — this is the Dhan
equivalent of the Groww "Phase 0b HARD-GATE" (CLAUDE.md §6).

## Honest-envelope flags

- INDIA VIX availability on Dhan's historical endpoint is
  **UNVERIFIED** — if absent, the revert's VIX-reference requirement
  (CLAUDE.md §1/§3) needs an operator decision, exactly as the Groww
  pivot flagged.

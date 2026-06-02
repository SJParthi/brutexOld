# ADR 0003 — Dhan-native instrument schema (drop old tables, rebuild)

- **Status:** Accepted (operator directive 2026-06-02)
- **Decision id:** DEC-DHAN-NATIVE-SCHEMA-001 (MIGRATION.md #399) +
  DEC-INSTRUMENT-PK-SEGMENT-001 (MIGRATION.md #400)
- **Builds on:** ADR 0002 (DEC-GROWW-DHAN-REVERT-001 — the live vendor is
  Dhan Data API v2)
- **Scope:** the DuckDB **instrument master** store
  (`brutex/data/instrument_store.py`) + the top-level
  `brutex.models.Instrument` dataclass. The Parquet **candle lake** (spot
  bars + expired-option bars) is UNCHANGED. The BRUTE-FORCE ENGINE is
  UNCHANGED (CLAUDE.md §2 Rule 3).

## Operator directive

> "drop the old tables and design the new tables as per our dhan docs
> alone only" … and (via AskUserQuestion) "Dhan column NAMES, keep
> BruteX's working VALUES" + "only the ~19 columns BruteX uses" (2026-06-02).

## Context

The pre-existing `instruments` table carried Groww-era column names
(`exchange`, `exchange_token`, `trading_symbol`, `vendor_symbol`, `name`,
`instrument_type`, `freeze_quantity`, `expiry_date`, `is_reserved`,
`internal_trading_symbol`, `buy_allowed`, `sell_allowed`) and keyed every
row on the **string** `vendor_symbol` (`"NSE-NIFTY"`). Two problems:

1. **Vendor mismatch** — the column names did not match the Dhan
   instrument-master CSV the data actually comes from
   (`docs/dhan-ref/09-instrument-master.md`), making every read a
   mental translation.
2. **Uniqueness was string-keyed**, not aligned to Dhan's real identity.
   Dhan addresses every instrument by the **pair** `(securityId,
   exchangeSegment)` on every API call — `security_id` is unique only
   WITHIN an exchange-segment (the same number can recur across `NSE_EQ`
   vs `NSE_FNO`, and across `NSE` vs `BSE`).

## Decision

**Drop and recreate** the store with Dhan-native column **names**, keeping
BruteX's proven engine **values** (`instrument` ∈ `IDX/EQ/FUT/CE/PE`;
`segment` ∈ `CASH/FNO`). `SCHEMA_VERSION` 2 → 3; on open the store runs
`DROP TABLE IF EXISTS …` then recreates — the master is 100%
re-derivable from one CSV re-download, so no real data is lost (the
candle lake is a separate store).

### Old → new column mapping (`Instrument` + `instruments` table)

| Old (Groww-era) | New (Dhan-native) | Dhan CSV field |
|---|---|---|
| `exchange` | `exch_id` | `EXCH_ID` |
| `exchange_token` | `security_id` (BIGINT) | `SECURITY_ID` |
| `trading_symbol` | `symbol_name` | `SYMBOL_NAME` |
| `name` | `display_name` | `DISPLAY_NAME` |
| `instrument_type` | `instrument` | `INSTRUMENT_TYPE` |
| `underlying_exchange_token` | `underlying_security_id` | `UNDERLYING_SECURITY_ID` |
| `freeze_quantity` | `sm_freeze_qty` | `SM_FREEZE_QTY` |
| `expiry_date` | `sm_expiry_date` | `SM_EXPIRY_DATE` |
| `is_reserved` | `asm_gsm_flag` | `ASM_GSM_FLAG` |
| `vendor_symbol` | **dropped** (engine looks up by `symbol_name`) | — |
| `internal_trading_symbol`, `buy_allowed`, `sell_allowed` | **dropped** | — |
| — | `option_type` 🆕 | `OPTION_TYPE` (`CE`/`PE`) |
| — | `expiry_flag` 🆕 | `EXPIRY_FLAG` (`M`/`W`) |

### Primary key = Dhan's own identity (DEC-INSTRUMENT-PK-SEGMENT-001)

The `instruments` PRIMARY KEY is **`(security_id, exch_id, segment,
snapshot_date)`** — i.e. `(securityId, exchangeSegment, snapshot)`, the
vendor's real identity. Both `exch_id` AND `segment` are load-bearing:

- without `segment`, `NSE_EQ` and `NSE_FNO` collide (same exchange, a
  `security_id` can recur across the cash vs F&O segment);
- without `exch_id`, `NSE_EQ` and `BSE_EQ` collide (same segment, NSE &
  BSE number their scrips independently — overlapping spaces).

Every "latest snapshot per instrument" query groups by
`(security_id, exch_id, segment)`, and `verify_integrity`'s duplicate
check counts `DISTINCT (security_id, exch_id, segment, snapshot_date)`.

## Consequences

- ✅ Reads match the Dhan CSV one-to-one; lookups are O(1) by the numeric
  `security_id` PK (no string keying).
- ✅ Uniqueness can never silently collide — it mirrors the vendor's
  `(securityId, exchangeSegment)` addressing exactly (covers the
  worst-case NSE/BSE id overlap, not just today's 243-row universe).
- ✅ The engine + lake are untouched; the rolling-option pull already
  addresses the underlying by the numeric `security_id`
  (DEC-OPTION-SECURITY-ID-FIX-001).
- 🟡 `vendor_symbol` (the exchange-prefixed `"NSE-NIFTY"` key) is gone;
  callers that need a global display key reconstruct it as
  `f"{exch_id}-{symbol_name}"`.
- 🟡 First open after upgrade drops + rebuilds the master (one CSV
  re-fetch repopulates it); the candle lake is unaffected.

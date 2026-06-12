# Groww instrument-master findings — Phase-0 §4.1 (instruments gate)

`DEC-DHAN-GROWW-REMIGRATE-001` (ADR-0009). Verified 2026-06-12 against the
operator-supplied Groww instrument master CSV (160,339 data rows; the live URL
is `https://growwapi-assets.groww.in/instruments/instrument.csv`, fetched fresh
by the adapter at ingest — this snapshot is point-in-time, expiries roll).

> **Scope of this evidence (Golden Rule #1, honest):** the CSV proves which
> instruments are **listed + addressable** by `groww_symbol`. It does **NOT**
> prove Groww serves **historical candles** for them (esp. India VIX back to
> 2020), the **rate-limit ceiling**, or **Dhan↔Groww drift** — those remain
> open Phase-0 gates that need the live `get_historical_candles` probe
> (ADR-0009 §4.2–§4.6) and gate the CUTOVER (PR-G), not the additive scaffold.

## Schema — 21 columns (8 more than the doc listed)

```
exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type,
segment, series, isin, underlying_symbol, underlying_exchange_token,
expiry_date, strike_price, lot_size, tick_size, freeze_quantity, is_reserved,
buy_allowed, sell_allowed, internal_trading_symbol, is_intraday
```

The 8 columns beyond `docs/groww/api-reference/02-instruments.md`'s list:
`series, isin, underlying_exchange_token, is_reserved, buy_allowed,
sell_allowed, internal_trading_symbol, is_intraday`. The adapter parser targets
this verified 21-column header.

## Brute-force universe — all 4 present (instruments gate: GO)

| Instrument | groww_symbol | name | instrument_type | segment |
|---|---|---|---|---|
| NIFTY 50 | `NSE-NIFTY` | NIFTY 50 | IDX | CASH |
| BANKNIFTY | `NSE-BANKNIFTY` | NIFTY Bank | IDX | CASH |
| SENSEX | `BSE-SENSEX` | Bse Sensex | IDX | CASH |
| **India VIX** | **`NSE-INDIAVIX`** | India Vix | IDX | CASH |

**India VIX is PRESENT** — this refutes the prior hostile review's
"~55-65% probability missing" concern (the make-or-break ADR-0009 risk #3, at
the instrument level). `buy_allowed=0 / sell_allowed=0` is expected — VIX is
reference-only (CLAUDE.md §2 Rule 3), never traded.

## Universe distribution (this snapshot)

- instrument_type: `PE` 73,867 · `CE` 73,834 · `EQ` 11,654 · `FUT` 954 · `IDX` 30
- segment: `FNO` 98,621 · `COMMODITY` 50,034 · `CASH` 11,684
  (COMMODITY is out of Track-1 scope; the adapter filters to NSE/BSE indices +
  equities — CLAUDE.md §3)

## Symbol formats (match `docs/groww/api-reference/03-backtesting.md`)

- Index/equity: `EXCHANGE-SYMBOL` (`NSE-NIFTY`, `BSE-SENSEX`)
- Option: `EXCHANGE-SYMBOL-EXPIRY-STRIKE-CE|PE` (e.g. `NSE-NIFTY-16Jun26-21800-CE`)
- Future: `EXCHANGE-SYMBOL-EXPIRY-FUT` (e.g. `NSE-NIFTY-25Aug26-FUT`)

## Gate status after this finding

| Phase-0 gate | Status |
|---|---|
| §4.1 India VIX **listed** | ✅ GO (`NSE-INDIAVIX`, IDX) |
| §4.1 NIFTY/BANKNIFTY/SENSEX listed | ✅ GO |
| Instrument schema / symbol formats | ✅ verified (21-col) |
| §4.1 India VIX **historical candles** served | 🟡 OPEN — live candle probe |
| §4.2 2020 1m granularity | 🟡 OPEN |
| §4.5 rate-limit / quota / token lifetime | 🟡 OPEN |
| §4.4 Dhan↔Groww drift | 🟡 OPEN |
| §4.6 options round-trip (with data) | 🟡 OPEN |

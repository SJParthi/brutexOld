---
title: Groww Trading API — Local Documentation Reference
fetched_at: 2026-05-26
api: Groww Trading API Python SDK
sdk_version_at_fetch: 1.5.0
purpose: Offline reference for BruteX Track 1 backtesting (post-DEC-VENDOR-PIVOT-001)
authority: This directory is the OFFICIAL source-of-truth for Groww API facts. CLAUDE.md §6 and docs/TRACK1_GROWW_ADAPTER.md cite these files. If drift is detected, these files WIN.
---

# Groww Trading API — Local Documentation Reference

Locally cached Python SDK documentation, fetched from the official Groww docs on **26 May 2026**. These files are the **authoritative source-of-truth** for all Groww API facts used by BruteX Track 1 (post-DEC-VENDOR-PIVOT-001).

## File Index

| File | Topic | Source URL |
|---|---|---|
| [01-introduction.md](./01-introduction.md) | SDK install, authentication (API Key + TOTP flows), rate limits | https://groww.in/trade-api/docs/python-sdk |
| [02-instruments.md](./02-instruments.md) | Instrument lookup APIs, CSV format, column schema | https://groww.in/trade-api/docs/python-sdk/instruments |
| [03-backtesting.md](./03-backtesting.md) | Historical candles, expiries, contracts, data window limits | https://groww.in/trade-api/docs/python-sdk/backtesting |
| [04-annexures.md](./04-annexures.md) | All SDK constants: exchanges, segments, order types, candle intervals | https://groww.in/trade-api/docs/python-sdk/annexures |
| [05-exceptions.md](./05-exceptions.md) | Custom exception hierarchy and error handling | https://groww.in/trade-api/docs/python-sdk/exceptions |
| [06-changelog.md](./06-changelog.md) | SDK version history and feature additions | https://groww.in/trade-api/docs/python-sdk/changelog |

---

## Quick Reference Card

### Rate Limits

| Type | APIs | Per Second | Per Minute |
|---|---|---|---|
| Orders | Create, Modify, Cancel | 10 | 250 |
| Live Data | Market Quote, LTP, OHLC | 10 | 300 |
| Non Trading | Order Status, Positions, Holdings, Margin | 20 | 500 |
| Live Feed (WebSocket) | Subscription cap | — | 1000 instruments concurrent |

> Rate limits are **type-level**: hitting one API in a type blocks all APIs in that type until window resets.
>
> **Per-day caps: NOT documented.** Backtesting endpoint bucket assignment also NOT explicitly listed in this table. Phase 0b empirical probe will resolve both questions.

### Authentication Flows

| Flow | Token Expiry | Use Case |
|---|---|---|
| API Key + Secret | Requires daily approval on Cloud API Keys page | ❌ REFUSED for BruteX (violates CLAUDE.md §11 AUTOMATED — "ZERO clicks per day") |
| TOTP | **No expiry** (TOTP token persistent) | ✅ Required for BruteX automated ingest |

### Backtesting Candle Window Limits (per single API call)

| Candle Interval | Max Duration per Request |
|---|---|
| 1 min, 2 min, 3 min, 5 min | **30 days** |
| 10 min, 15 min, 30 min | 90 days |
| 1 hour, 4 hours, 1 day, 1 week, 1 month | 180 days |

> For data spanning longer periods, paginate by making multiple calls. BruteX Track 1 uses **30-day chunks for 1-minute candles** per Track 1 ingest design.

### Historical Data Availability

- **Equities, Indices, FNO**: From 2020
- **Backtesting APIs**: Only **CASH** and **FNO** segments supported
- **Commodity (MCX)**: Trading supported (v1.5.0+), but NOT supported for backtesting

### Supported Markets

| Market | Trading | Backtesting |
|---|---|---|
| NSE | ✅ | ✅ |
| BSE | ✅ | ✅ |
| MCX | ✅ (since v1.5.0, Dec 2025) | ❌ (no backtest) |

### Segments

| Segment | Constant | Trading | Backtesting |
|---|---|---|---|
| CASH (Equity) | `groww.SEGMENT_CASH` | ✅ | ✅ |
| FNO (Derivatives) | `groww.SEGMENT_FNO` | ✅ | ✅ |
| COMMODITY | `groww.SEGMENT_COMMODITY` | ✅ | ❌ |

### Groww Symbol Format

| Instrument Type | Format | Example |
|---|---|---|
| Stock | `EXCHANGE-SYMBOL` | `NSE-RELIANCE` |
| Index | `EXCHANGE-SYMBOL` | `NSE-NIFTY`, `BSE-SENSEX` |
| Future | `EXCHANGE-SYMBOL-EXPIRY-FUT` | `NSE-NIFTY-30Sep25-FUT` |
| Call Option | `EXCHANGE-SYMBOL-EXPIRY-STRIKE-CE` | `NSE-NIFTY-30Sep25-24650-CE` |
| Put Option | `EXCHANGE-SYMBOL-EXPIRY-STRIKE-PE` | `NSE-NIFTY-30Sep25-24650-PE` |

**Expiry format:** `DDMmmYY` (e.g., `30Sep25` for 30 Sep 2025).

### Key SDK Constants Quick Lookup

| Purpose | Constant |
|---|---|
| NSE | `groww.EXCHANGE_NSE` |
| BSE | `groww.EXCHANGE_BSE` |
| MCX | `groww.EXCHANGE_MCX` |
| Equity segment | `groww.SEGMENT_CASH` |
| F&O segment | `groww.SEGMENT_FNO` |
| 1-min candle | `groww.CANDLE_INTERVAL_MIN_1` |
| 5-min candle | `groww.CANDLE_INTERVAL_MIN_5` |
| 15-min candle | `groww.CANDLE_INTERVAL_MIN_15` |
| Daily candle | `groww.CANDLE_INTERVAL_DAY` |
| Limit order | `groww.ORDER_TYPE_LIMIT` |
| Market order | `groww.ORDER_TYPE_MARKET` |
| Stop loss | `groww.ORDER_TYPE_STOP_LOSS` |
| Buy | `groww.TRANSACTION_TYPE_BUY` |
| Sell | `groww.TRANSACTION_TYPE_SELL` |
| Day validity | `groww.VALIDITY_DAY` |
| Cash & Carry | `groww.PRODUCT_CNC` |
| Intraday | `groww.PRODUCT_MIS` |
| Normal margin | `groww.PRODUCT_NRML` |

### Exception Hierarchy (Quick Lookup)

```
GrowwBaseException
├── GrowwAPIException
│   ├── GrowwAPIAuthenticationException
│   ├── GrowwAPIAuthorisationException
│   ├── GrowwAPIBadRequestException
│   ├── GrowwAPINotFoundException
│   ├── GrowwAPIRateLimitException
│   └── GrowwAPITimeoutException
└── GrowwFeedException
    ├── GrowwFeedConnectionException
    └── GrowwFeedNotSubscribedException
```

### Instrument Types

| Value | Description |
|---|---|
| EQ | Equity — Represents ownership in a company |
| IDX | Index — Composite value of a group of stocks |
| FUT | Futures — Derivatives contract to buy/sell at a future date |
| CE | Call Option |
| PE | Put Option |

### Candle Response Format

Each candle row: `[timestamp, open, high, low, close, volume, open_interest]`

- `timestamp` (string): `yyyy-MM-dd HH:mm:ss` format
- `open, high, low, close` (float): **in rupees** — BruteX parser MUST `int(round(price * 100))` at boundary to preserve int64 paisa invariant
- `volume` (int)
- `open_interest`: only returned for FNO instruments; `null` for others

### Instrument CSV Schema (17 columns)

| Column | Type | Notes |
|---|---|---|
| exchange | string | NSE, BSE, MCX |
| exchange_token | string | Unique token from exchange |
| trading_symbol | string | Order-placement symbol |
| groww_symbol | string | Backtesting symbol |
| name | string | Company / instrument name |
| instrument_type | string | EQ, IDX, FUT, CE, PE |
| segment | string | CASH, FNO, etc. |
| series | string | EQ, A, B, ... |
| isin | string | ISIN code |
| underlying_symbol | string | For derivatives; empty for stocks/indices |
| underlying_exchange_token | string | For derivatives |
| lot_size | number | Min lot size |
| expiry_date | string | For derivatives |
| strike_price | number | For options |
| tick_size | number | Min price movement |
| freeze_quantity | number | Frozen quantity |
| is_reserved | boolean | Reserved for trading |
| buy_allowed | boolean | Buy permitted |
| sell_allowed | boolean | Sell permitted |

### SDK Install

```bash
pip install growwapi          # base install (Python 3.9+)
pip install pyotp             # required for TOTP auth flow
pip install --upgrade growwapi
```

### Latest Version at Fetch Time

**v1.5.0** released 3 December 2025 — added Commodity (MCX) trading support.

---

## Critical Notes for BruteX Track 1 (post-DEC-VENDOR-PIVOT-001)

1. **Auth must use TOTP flow.** API Key + Secret flow requires daily approval → violates CLAUDE.md §11 AUTOMATED rule "ZERO clicks per day".

2. **Prices are in RUPEES (float).** BruteX parser MUST `int(round(price * 100))` at the parser boundary to preserve int64 paisa invariant. This is a P0 conversion documented in DEC-VENDOR-PIVOT-001.

3. **Backtesting endpoint rate-limit bucket: UNVERIFIED.** The rate-limits table lists only Orders / Live Data / Non Trading. Backtesting endpoints (`get_historical_candles`, `get_expiries`, `get_contracts`) are NOT explicitly bucketed. BruteX uses adaptive [4..8] r/s rate-limiter per `docs/TRACK1_GROWW_ADAPTER.md §5` to discover the practical cap empirically.

4. **INDIA VIX presence: UNVERIFIED until Phase 0b probe.** Sample CSV rows show BANKNIFTY options + futures but no explicit INDIA VIX row. Phase 0b empirical probe grep on the instruments CSV is the **hard gate** before any Groww adapter code lands.

5. **30-day chunk for 1m candles.** This is the documented limit. BruteX chunk planner uses 30-day chunks for 1m intervals.

6. **No per-day rate cap documented.** Type-level caps (10/sec, 300/min for Live Data) only.

---

## SEBI Compliance Note

As per Groww dashboard alert: **Static IP registration was mandatory before 31 March 2026** for placing orders. This deadline has passed.

**BruteX Track 1 is signal-only research** (per CLAUDE.md §1) — it does NOT place orders. So static IP is **NOT REQUIRED** for BruteX Track 1 ingest. The requirement would apply only if/when a future Live-Trading Track is built.

---

## Useful Links

- Instruments CSV (full): https://growwapi-assets.groww.in/instruments/instrument.csv
- API Keys dashboard: https://groww.in/trade-api/api-keys
- Trading API subscription page: https://groww.in/user/profile/trading-apis
- Official docs index: https://groww.in/trade-api/docs

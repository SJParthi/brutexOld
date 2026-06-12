# 03 — Backtesting · Groww Trading API (Python SDK)

> **Source:** https://groww.in/trade-api/docs/python-sdk/backtesting
> **Verified:** 12 June 2026

Fetch historical candle data and instrument information for backtesting trading strategies.

> **Note:** Currently, Backtesting APIs only support **CASH and FNO** segments.

## Groww Symbol (construction)

A Groww symbol is an easy-to-construct unique identifier for an instrument across exchanges and segments. It is formed by concatenating:

- **Exchange** — where the instrument is traded
- **Trading Symbol** — the name/ticker of the instrument
- **Expiry Date** — only for derivatives (format: `DDMmmYY`, example: `23Jan25`)
- **Strike Price** — only for options (the target price level)
- **Option Type** — only for derivatives:
  - `CE` = Call Option
  - `PE` = Put Option
  - `FUT` = Futures

Rules:
- **Stocks & Indices:** only exchange and trading symbol are used.
- **Futures:** exchange, trading symbol, expiry date, and `FUT`.
- **Options:** all components including strike price and option type (CE/PE).

Examples:
| Type | Examples |
| --- | --- |
| Equity | `NSE-WIPRO`, `BSE-RELIANCE` |
| Index | `NSE-NIFTY`, `BSE-SENSEX` |
| Future | `NSE-NIFTY-30Sep25-FUT`, `BSE-SENSEX-25Sep25-FUT` |
| Call Option | `NSE-NIFTY-30Sep25-24650-CE`, `BSE-SENSEX-25Sep25-79500-CE` |
| Put Option | `NSE-NIFTY-30Sep25-24650-PE`, `BSE-SENSEX-25Sep25-79500-PE` |

> The Groww symbol also exists in the instruments CSV and can be obtained from the Get Instruments API.

---

## Get Expiries — `groww.get_expiries(...)`

Retrieves available expiry dates for derivatives (FNO) instruments for a given exchange and underlying symbol. Useful for backtesting options and futures strategies. **FNO data available from 2020.**

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

expiries_response = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=1
)
print(expiries_response)
```

**Request Schema**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `exchange` | string | ✅ | Stock exchange (NSE, BSE) |
| `underlying_symbol` | string | ✅ | Underlying symbol for which expiry dates are required (e.g., NIFTY, BANKNIFTY, RELIANCE) |
| `year` | integer | ❌ | Year (2020 – current year). If not specified, current year is considered. |
| `month` | integer | ❌ | Month (1–12). If not specified, expiries of the entire year are returned. |

**Response Payload**
```json
{
    "expiries" : [
        "2024-01-25",
        "2024-01-31",
        "2024-02-29",
        "2024-03-28"
    ]
}
```

**Response Schema**

| Name | Type | Description |
| --- | --- | --- |
| `expiries` | array[string] | Array of expiry dates in `YYYY-MM-DD` format |

---

## Get Contracts — `groww.get_contracts(...)`

Retrieves available contract symbols (Groww symbols) for derivatives instruments for a given exchange, underlying symbol, and expiry date. Essential for backtesting specific options/futures contracts by passing them into the Candles API. **FNO data available from 2020.**

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

contracts_response = groww.get_contracts(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    expiry_date="2025-01-25"
)
print(contracts_response)
```

**Request Schema**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `exchange` | string | ✅ | Stock exchange (NSE, BSE) |
| `underlying_symbol` | string | ✅ | Underlying symbol for which contracts are required (1–20 characters) |
| `expiry_date` | string | ✅ | Expiry date in `YYYY-MM-DD` format |

**Response Payload**
```json
{
    "contracts": [
        "NSE-NIFTY-02Jan25-28500-PE",
        "NSE-NIFTY-02Jan25-24000-PE",
        "NSE-NIFTY-02Jan25-26800-PE",
        "NSE-NIFTY-02Jan25-27450-PE",
        "NSE-NIFTY-02Jan25-19050-PE",
        "NSE-NIFTY-02Jan25-22300-PE",
        "NSE-NIFTY-02Jan25-28150-CE"
    ]
}
```

**Response Schema**

| Name | Type | Description |
| --- | --- | --- |
| `contracts` | array[string] | Array of Groww symbols of the contracts available for the given expiry date |

---

## Get Historical Candles — `groww.get_historical_candles(...)`

Fetch historical candle data for backtesting. Provides:
- Historical **OHLC** (Open, High, Low, Close) for all instruments
- **Volume** for tradable instruments (Equities and FNO)
- **Open Interest (OI)** for FNO

**Data of Equities, Indices and FNO instruments available from 2020.**

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

historical_candles_response = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    groww_symbol="NSE-WIPRO",
    start_time="2025-09-24 10:56:00",
    end_time="2025-09-24 12:00:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_30
)
print(historical_candles_response)


# OR — use expiries and contracts to get historical data of FNO instruments
jan2024_nifty_expiries = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=1
)
print("NIFTY Expiries in Jan 2024:", jan2024_nifty_expiries)

nifty_24_jan_contracts = groww.get_contracts(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    expiry_date=jan2024_nifty_expiries['expiries'][0]  # first expiry date from the list
)
print("NIFTY Contracts in Jan 2024:", nifty_24_jan_contracts)

candles = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_FNO,
    groww_symbol=nifty_24_jan_contracts['contracts'][0],  # first contract from the list
    start_time="2024-01-01 09:15:00",
    end_time="2024-01-10 15:30:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_15
)
print(candles)
```

**Request Schema**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `exchange` | string | ✅ | Stock exchange |
| `segment` | string | ✅ | Segment such as CASH, FNO, etc. |
| `groww_symbol` | string | ✅ | Groww symbol of the instrument |
| `start_time` | string | ✅ | Start time in `yyyy-MM-dd HH:mm:ss` **or** epoch seconds |
| `end_time` | string | ✅ | End time in `yyyy-MM-dd HH:mm:ss` **or** epoch seconds |
| `candle_interval` | string | ✅ | Interval for which data is required |

**Response Payload** (all prices in rupees)
```json
{
  "candles": [
      [
          "2025-09-24T10:30:00",  // candle timestamp in yyyy-MM-dd HH:mm:ss format
          245.95,                 // open price
          246.15,                 // high price
          245.05,                 // low price
          245.6,                  // close price
          735060,                 // volume
          null                    // open interest (only for FNO instruments, null for others)
      ],
      ["2025-09-24T11:00:00", 245.64, 245.66, 244.8, 244.94, 682373, null],
      ["2025-09-24T11:30:00", 244.95, 245.28, 244.6, 245.13, 353800, null],
      ["2025-09-24T12:00:00", 245.12, 245.5,  244.9, 245.4,  254058, null]
  ],
  "closing_price": 245.40,
  "start_time": "2025-09-24 10:30:00",
  "end_time": "2025-09-24 12:00:00",
  "interval_in_minutes": 30
}
```

**Response Schema**

| Name | Type | Description |
| --- | --- | --- |
| `candles` | array[array] | Each candle: timestamp (`yyyy-MM-dd HH:mm:ss`), open, high, low, close, volume, open interest |
| `closing_price` | float | Closing price of the instrument |
| `start_time` | string | Start time in `yyyy-MM-dd HH:mm:ss` format |
| `end_time` | string | End time in `yyyy-MM-dd HH:mm:ss` format |
| `interval_in_minutes` | int | Interval in minutes |

> **Candle array index order:** `[0] timestamp · [1] open · [2] high · [3] low · [4] close · [5] volume · [6] open_interest`. OI is `null` for non-FNO instruments.

## Backtesting Data Limits (max duration per request)

| Candle Intervals | Max Duration per Request |
| --- | --- |
| 1 min, 2 min, 3 min, 5 min | **30 days** |
| 10 min, 15 min, 30 min | **90 days** |
| 1 hour, 4 hours, 1 day, 1 week, 1 month | **180 days** |

> For longer ranges, chunk requests to stay within these caps per call.

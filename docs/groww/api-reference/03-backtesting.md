---
source_url: https://groww.in/trade-api/docs/python-sdk/backtesting
fetched_at: 2026-05-26
api: Groww Trading API Python SDK
section: Backtesting
---

# Backtesting

Fetch historical candle data and instrument information for backtesting trading strategies using Groww APIs.

> **Note:** Currently, Backtesting APIs only support **CASH** and **FNO** segments.

## Groww Symbol

Groww symbol is an easy-to-construct unique identifier for an instrument across exchanges and segments. It is formed by concatenating:

- **Exchange** — Where the instrument is traded
- **Trading Symbol** — The name/ticker of the instrument
- **Expiry Date** — Only for derivatives (format: `DDMmmYY`, e.g., `23Jan25`)
- **Strike Price** — Only for options (the target price level)
- **Option Type** — Only for derivatives:
  - `CE` = Call Option
  - `PE` = Put Option
  - `FUT` = Futures

**For Stocks and Indices:** Only exchange and trading symbol are used.

**For Futures:** Exchange, trading symbol, expiry date, and "FUT" are used.

**For Options:** All components are used including strike price and option type (CE/PE).

### Examples

| Type | Format | Example |
|---|---|---|
| Equity | `EXCHANGE-SYMBOL` | `NSE-WIPRO`, `BSE-RELIANCE` |
| Index | `EXCHANGE-SYMBOL` | `NSE-NIFTY`, `BSE-SENSEX` |
| Future | `EXCHANGE-SYMBOL-EXPIRY-FUT` | `NSE-NIFTY-30Sep25-FUT`, `BSE-SENSEX-25Sep25-FUT` |
| Call Option | `EXCHANGE-SYMBOL-EXPIRY-STRIKE-CE` | `NSE-NIFTY-30Sep25-24650-CE`, `BSE-SENSEX-25Sep25-79500-CE` |
| Put Option | `EXCHANGE-SYMBOL-EXPIRY-STRIKE-PE` | `NSE-NIFTY-30Sep25-24650-PE`, `BSE-SENSEX-25Sep25-79500-PE` |

> Groww symbol also exists in the instruments CSV file and can be obtained from the [Get Instruments](https://groww.in/trade-api/docs/python-sdk/instruments) API.

---

## Get Expiries

This API retrieves available expiry dates for derivatives instruments (FNO) for a given exchange and underlying symbol. Useful for backtesting options and futures strategies.

> **Data of FNO instruments are available from 2020.**

### Python SDK Usage

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

### Request Schema

| Name | Type | Description |
|---|---|---|
| `exchange` * | string | Stock exchange (NSE, BSE) |
| `underlying_symbol` * | string | Underlying symbol (e.g., NIFTY, BANKNIFTY, RELIANCE) |
| `year` | integer | Year for which expiry dates are required (2020 - current). If not specified, current year is used. |
| `month` | integer | Month (1-12). If not specified, expiries of the entire year are returned. |

`*` required parameters

### Response Payload

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

### Response Schema

| Name | Type | Description |
|---|---|---|
| expiries | array[string] | Array of expiry dates in YYYY-MM-DD format |

---

## Get Contracts

This API retrieves available contract symbols for derivatives instruments for a given exchange, underlying symbol, and expiry date. Essential for backtesting specific options or futures contracts by passing them in the Candles API.

> **Data of FNO instruments are available from 2020.**

### Python SDK Usage

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

### Request Schema

| Name | Type | Description |
|---|---|---|
| `exchange` * | string | Stock exchange (NSE, BSE) |
| `underlying_symbol` * | string | Underlying symbol (1-20 characters) |
| `expiry_date` * | string | Expiry date in YYYY-MM-DD format |

`*` required parameters

### Response Payload

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

### Response Schema

| Name | Type | Description |
|---|---|---|
| contracts | array[string] | Array of Groww symbols of contracts available for the given expiry date |

---

## Get Historical Candles

Fetch historical candle data for backtesting trading strategies. This API provides:

- Historical OHLC (Open, High, Low, Close) data for all instruments
- Volume for tradable instruments (Equities and FNO)
- Open Interest (OI) for FNO

> **Data of Equities, Indices and FNO instruments are available from 2020.**

### Python SDK Usage

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

# Example 1: Equity historical candles
historical_candles_response = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    groww_symbol="NSE-WIPRO",
    start_time="2025-09-24 10:56:00",
    end_time="2025-09-24 12:00:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_30
)
print(historical_candles_response)


# Example 2: FNO historical candles via expiries + contracts
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
    expiry_date=jan2024_nifty_expiries['expiries'][0]  # First expiry date from list
)

print("NIFTY Contracts in Jan 2024:", nifty_24_jan_contracts)

candles = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_FNO,
    groww_symbol=nifty_24_jan_contracts['contracts'][0],  # First contract from list
    start_time="2024-01-01 09:15:00",
    end_time="2024-01-10 15:30:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_15
)
print(candles)
```

### Request Schema

| Name | Type | Description |
|---|---|---|
| `exchange` * | string | Stock exchange |
| `segment` * | string | Segment (CASH, FNO, etc.) |
| `groww_symbol` * | string | Groww symbol of the instrument |
| `start_time` * | string | Start time in `yyyy-MM-dd HH:mm:ss` or epoch seconds format |
| `end_time` * | string | End time in `yyyy-MM-dd HH:mm:ss` or epoch seconds format |
| `candle_interval` * | string | Interval for which data is required |

`*` required parameters

### Response Payload

All prices in rupees.

```json
{
  "candles": [
      [
          "2025-09-24T10:30:00",
          245.95,
          246.15,
          245.05,
          245.6,
          735060,
          null
      ],
      [
          "2025-09-24T11:00:00",
          245.64,
          245.66,
          244.8,
          244.94,
          682373,
          null
      ],
      [
          "2025-09-24T11:30:00",
          244.95,
          245.28,
          244.6,
          245.13,
          353800,
          null
      ],
      [
          "2025-09-24T12:00:00",
          245.12,
          245.5,
          244.9,
          245.4,
          254058,
          null
      ]
  ],
  "closing_price": 245.40,
  "start_time": "2025-09-24 10:30:00",
  "end_time": "2025-09-24 12:00:00",
  "interval_in_minutes": 30
}
```

**Each candle row format:** `[timestamp, open, high, low, close, volume, open_interest]`

> `open_interest` is only returned for FNO instruments; `null` for others.

### Response Schema

| Name | Type | Description |
|---|---|---|
| candles | array[array] | Array of candle data. Each candle: `[timestamp (yyyy-MM-dd HH:mm:ss), open, high, low, close, volume, open_interest]` |
| closing_price | float | Closing price of the instrument |
| start_time | string | Start time in yyyy-MM-dd HH:mm:ss format |
| end_time | string | End time in yyyy-MM-dd HH:mm:ss format |
| interval_in_minutes | int | Interval in minutes |

---

## Backtesting Data Limits

Maximum date range that can be fetched in a single API call:

| Candle Intervals | Max Duration per Request |
|---|---|
| **1 min, 2 min, 3 min, 5 min** | 30 days |
| **10 min, 15 min, 30 min** | 90 days |
| **1 hour, 4 hours, 1 day, 1 week, 1 month** | 180 days |

> For data spanning longer periods, paginate by making multiple calls.

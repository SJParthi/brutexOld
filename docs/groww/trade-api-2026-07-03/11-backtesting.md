# Groww Trade API — Backtesting

> **Source:** https://groww.in/trade-api/docs/python-sdk/backtesting
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Backtesting

Fetch historical candle data and instrument information for backtesting trading strategies using Groww APIs

> **Note:** Currently, Backtesting APIs only support CASH and FNO segments.

## [Groww Symbol](#groww-symbol)

Groww symbol is a easy to construct unique identifier for an instrument across exchanges and segments. It is formed by concatenating

- **Exchange** - Where the instrument is traded
- **Trading Symbol** - The name/ticker of the instrument
- **Expiry Date** - Only for derivatives (format: DDMmmYY, example: 23Jan25)
- **Strike Price** - Only for options (the target price level)
- **Option Type** - Only for derivatives:
  - CE = Call Option
  - PE = Put Option
  - FUT = Futures

**For Stocks and Indices:** Only exchange and trading symbol are used.

**For Futures:** Exchange, trading symbol, expiry date, and "FUT" are used.

**For Options:** All components are used including strike price and option type (CE/PE).

For example:

- Equity: `NSE-WIPRO`, `BSE-RELIANCE`
- Index: `NSE-NIFTY`, `BSE-SENSEX`
- Future: `NSE-NIFTY-30Sep25-FUT`, `BSE-SENSEX-25Sep25-FUT`
- Call Option: `NSE-NIFTY-30Sep25-24650-CE`, `BSE-SENSEX-25Sep25-79500-CE`
- Put Option: `NSE-NIFTY-30Sep25-24650-PE`, `BSE-SENSEX-25Sep25-79500-PE`

Groww symbol also exists in the instruments csv file and it can be obtained from the [Get Instruments](/trade-api/docs/python-sdk/instruments) API.

## [Get Expiries](#get-expiries)

This API retrieves available expiry dates for derivatives instruments (FNO) for a given exchange and underlying symbol. Useful for backtesting options and futures strategies. Data of FNO instruments are available from 2020.

### [Python SDK Usage](#python-sdk-usage)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
expiries_response = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=1
)
print(expiries_response)
```

#### [Request Schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) (NSE, BSE) |
| underlying\_symbol `*` | string | Underlying symbol for which expiry dates are required (e.g., NIFTY, BANKNIFTY, RELIANCE) |
| year | integer | Year for which expiry dates are required (2020 - current year). If year is not specified, current year is considered. |
| month | integer | Month for which expiry dates are required (1-12). If month is not specified, expiries of the entire year is returned. |

`*`required parameters

### [Response Payload](#response-payload)

```
{
    "expiries" : [
        "2024-01-25",
        "2024-01-31",
        "2024-02-29",
        "2024-03-28"
    ]
}
```

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| expiries | array[string] | Array of expiry dates in YYYY-MM-DD format |

## [Get Contracts](#get-contracts)

This API retrieves available contract symbols for derivatives instruments for a given exchange, underlying symbol, and expiry date. Essential for backtesting specific options or futures contracts by passing them in [Candles API](#get-historical-candle-data). Data of FNO instruments are available from 2020.

### [Python SDK Usage](#python-sdk-usage-1)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
contracts_response = groww.get_contracts(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    expiry_date="2025-01-25"
)
print(contracts_response)
```

#### [Request Schema](#request-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) (NSE, BSE) |
| underlying\_symbol `*` | string | Underlying symbol for which contracts are required (1-20 characters) |
| expiry\_date `*` | string | Expiry date in YYYY-MM-DD format for which contracts are required |

`*`required parameters

### [Response Payload](#response-payload-1)

```
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

#### [Response Schema](#response-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| contracts | array[string] | Array of groww symbols of the contracts available for the given expiry date |

## [Get Historical Candles](#get-historical-candles)

Fetch historical candle data for backtesting trading strategies. This API provides

- Historical OHLC (Open, High, Low, Close) data for all instruments
- Volume for tradable instruments (Equities and FNO)
- Open Interest (OI) for FNO

Data of Equities, Indices and FNO instruments are available from 2020.

### [Python SDK Usage](#python-sdk-usage-2)

```
from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"
 
# Initialize Groww API
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
 
 
# OR
# You can also use expiries and contracts API to get historical data of FNO instruments
 
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
    expiry_date=jan2024_nifty_expiries['expiries'][0]  # Using the first expiry date from the list
)
 
print("NIFTY Contracts in Jan 2024:", nifty_24_jan_contracts)
 
candles = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_FNO,
    groww_symbol=nifty_24_jan_contracts['contracts'][0],  # Using the first contract from the list
    start_time="2024-01-01 09:15:00",
    end_time="2024-01-10 15:30:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_15
)
print(candles)
```

#### [Request Schema](#request-schema-2)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) |
| segment `*` | string | [Segment](/trade-api/docs/python-sdk/annexures#segment) of the instrument such as CASH, FNO etc. |
| groww\_symbol `*` | string | Groww symbol of the instrument for which historical data is required |
| start\_time `*` | string | Start time in yyyy-MM-dd HH:mm:ss or epoch seconds format from which data is required |
| end\_time `*` | string | End time in yyyy-MM-dd HH:mm:ss or epoch seconds format until which data is required |
| candle\_interval `*` | string | [Interval](/trade-api/docs/python-sdk/annexures#candle-interval) for which data is required |

`*`required parameters

### [Response Payload](#response-payload-2)

All prices in rupees.

```
{
  "candles": [
      [
          "2025-09-24T10:30:00", // candle timestamp in yyyy-MM-dd HH:mm:ss format
          245.95, // open price
          246.15, // high price
          245.05, // low price
          245.6,  // close price
          735060, // volume
          null // open interest (only for FNO instruments, null for others)
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

#### [Response Schema](#response-schema-2)

| Name | Type | Description |
| --- | --- | --- |
| candles | array[array] | Array of candle data. Each candle contains: timestamp (yyyy-MM-dd HH:mm:ss), open, high, low, close, volume, open interest |
| closing\_price | float | Closing price of the instrument |
| start\_time | string | Start time in yyyy-MM-dd HH:mm:ss format |
| end\_time | string | End time in yyyy-MM-dd HH:mm:ss format |
| interval\_in\_minutes | int | Interval in minutes |

## [Backtesting Data Limits](#backtesting-data-limits)

| Candle Intervals | Max Duration per Request |
| --- | --- |
| **1 min, 2 min, 3 min, 5 min** | 30 days |
| **10 min, 15 min, 30 min** | 90 days |
| **1 hour, 4 hours, 1 day, 1 week, 1 month** | 180 days |

[Previous

Historical Data](/trade-api/docs/python-sdk/historical-data)[Next

Feed](/trade-api/docs/python-sdk/feed)

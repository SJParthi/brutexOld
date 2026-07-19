# Groww Trading API — Backtesting (Expiries, Contracts, Historical Candles)

> Sources:
> - https://groww.in/trade-api/docs/curl/backtesting
> - https://groww.in/trade-api/docs/python-sdk/backtesting
> Captured: 2026-07-15

Fetch historical candle data and instrument information for backtesting trading strategies using Groww APIs.

> **Note:** Currently, Backtesting APIs only support CASH and FNO segments.

Endpoints summary:

| Operation | Method + Path | Python SDK method |
| --- | --- | --- |
| Get Expiries | `GET /v1/historical/expiries` | `groww.get_expiries(...)` |
| Get Contracts | `GET /v1/historical/contracts` | `groww.get_contracts(...)` |
| Get Historical Candle Data | `GET /v1/historical/candles` | `groww.get_historical_candles(...)` |

---

## Groww Symbol

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

Groww symbol also exists in the instruments csv file and it can be obtained from the [Instruments](./03-instruments.md) API.

---

## Get Expiries

`GET https://api.groww.in/v1/historical/expiries`

This API retrieves available expiry dates for derivatives instruments (FNO) for a given exchange and underlying symbol. Useful for backtesting options and futures strategies. **Data of FNO instruments are available from 2020.**

### cURL Request

```bash
# You can also use wget
curl -X GET 'https://api.groww.in/v1/historical/expiries?exchange=NSE&underlying_symbol=NIFTY&year=2024&month=1' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
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

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) (NSE, BSE) |
| underlying_symbol `*` | string | Underlying symbol for which expiry dates are required (e.g., NIFTY, BANKNIFTY, RELIANCE) |
| year | integer | Year for which expiry dates are required (2020 - current year). If year is not specified, current year is considered. |
| month | integer | Month for which expiry dates are required (1-12). If month is not specified, expiries of the entire year is returned. |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "expiries" : [
      "2024-01-25",
      "2024-01-31",
      "2024-02-29",
      "2024-03-28"
    ]
  }
}
```

Response schema:

| Name | Type | Description |
| --- | --- | --- |
| expiries | array[string] | Array of expiry dates in YYYY-MM-DD format |

---

## Get Contracts

`GET https://api.groww.in/v1/historical/contracts`

This API retrieves available contract symbols for derivatives instruments for a given exchange, underlying symbol, and expiry date. Essential for backtesting specific options or futures contracts (by passing them into the Candles API). **Data of FNO instruments are available from 2020.**

### cURL Request

```bash
# You can also use wget
curl -X GET 'https://api.groww.in/v1/historical/contracts?exchange=NSE&underlying_symbol=NIFTY&expiry_date=2025-01-25' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
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

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) (NSE, BSE) |
| underlying_symbol `*` | string | Underlying symbol for which contracts are required (1-20 characters) |
| expiry_date `*` | string | Expiry date in YYYY-MM-DD format for which contracts are required |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "contracts" : [
      "NSE-NIFTY-02Jan25-28500-PE",
      "NSE-NIFTY-02Jan25-24000-PE",
      "NSE-NIFTY-02Jan25-26800-PE",
      "NSE-NIFTY-02Jan25-27450-PE",
      "NSE-NIFTY-02Jan25-19050-PE",
      "NSE-NIFTY-02Jan25-22300-PE",
      "NSE-NIFTY-02Jan25-28150-CE"
    ]
  }
}
```

Response schema:

| Name | Type | Description |
| --- | --- | --- |
| contracts | array[string] | Array of groww symbols of the contracts available for the given expiry date |

---

## Get Historical Candle Data

`GET https://api.groww.in/v1/historical/candles`

Fetch historical candle data for backtesting trading strategies. This API provides:

- Historical OHLC (Open, High, Low, Close) data for all instruments
- Volume for tradable instruments (Equities and FNO)
- Open Interest (OI) for FNO

**Data of Equities, Indices and FNO instruments are available from 2020.**

### cURL Request

```bash
# You can also use wget
curl -X GET 'https://api.groww.in/v1/historical/candles?exchange=NSE&segment=CASH&groww_symbol=NSE-WIPRO&start_time=2025-09-24 10:56:00&end_time=2025-09-24 15:21:00&candle_interval=5minute' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage

```python
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

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) (NSE, BSE) |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO etc. |
| groww_symbol `*` | string | Groww symbol of the instrument for which historical data is required |
| start_time `*` | string | Start time in yyyy-MM-dd HH:mm:ss or epoch seconds format from which data is required |
| end_time `*` | string | End time in yyyy-MM-dd HH:mm:ss or epoch seconds format until which data is required |
| candle_interval `*` | string | [Interval](./13-annexures.md#candle-interval) for which data is required |

`*` required parameters

### Response

All prices in rupees.

```json
{
    "status": "SUCCESS",
    "payload": {
        "candles": [
            ["2025-09-24T10:30:00", 245.95, 246.15, 245.05, 245.6, 735060, null],
            ["2025-09-24T11:00:00", 245.64, 245.66, 244.8, 244.94, 682373, null],
            ["2025-09-24T11:30:00", 244.95, 245.28, 244.6, 245.13, 353800, null],
            ["2025-09-24T12:00:00", 245.12, 245.5, 244.9, 245.4, 254058, null],
            ["2025-09-24T12:30:00", 245.42, 245.5, 244.75, 244.9, 323824, null],
            ["2025-09-24T13:00:00", 244.88, 245.25, 244.8, 244.97, 324619, null],
            ["2025-09-24T13:30:00", 245.04, 245.09, 244.5, 244.6, 280445, null]
        ],
        "closing_price": 244.6,
        "start_time": "2025-09-24 10:30:00",
        "end_time": "2025-09-24 13:30:00",
        "interval_in_minutes": 30
    }
}
```

Candle array order (documented inline): `[candle timestamp in yyyy-MM-dd HH:mm:ss format, open price, high price, low price, close price, volume, open interest (only for FNO instruments, null for others)]`.

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| candles | array[array] | Array of candle data. Each candle contains: timestamp (yyyy-MM-dd HH:mm:ss), open, high, low, close, volume, open interest |
| closing_price | float | Closing price of the instrument |
| start_time | string | Start time in yyyy-MM-dd HH:mm:ss format |
| end_time | string | End time in yyyy-MM-dd HH:mm:ss format |
| interval_in_minutes | int | Interval in minutes |

### Using Expiries and Contracts APIs for FNO Backtesting (cURL workflow)

Step 1: Get Available Expiries

```bash
# Get NIFTY expiries for January 2024
curl -X GET 'https://api.groww.in/v1/historical/expiries?exchange=NSE&underlying_symbol=NIFTY&year=2024&month=1' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Step 2: Get Contracts for Specific Expiry

```bash
# Get all NIFTY contracts for 4th January 2024 expiry
curl -X GET 'https://api.groww.in/v1/historical/contracts?exchange=NSE&underlying_symbol=NIFTY&expiry_date=2024-01-04' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

Step 3: Fetch Historical Candles for Specific Contract

```bash
# Get historical candles for NIFTY 19200 CE option
curl -X GET 'https://api.groww.in/v1/historical/candles?exchange=NSE&segment=FNO&groww_symbol=NSE-NIFTY-04Jan24-19200-CE&start_time=2024-01-01 09:15:00&end_time=2024-01-10 15:30:00&candle_interval=15minute' \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

## Backtesting Data Limits

| Candle Intervals | Max Duration per Request |
| --- | --- |
| **1 min, 2 min, 3 min, 5 min** | 30 days |
| **10 min, 15 min, 30 min** | 90 days |
| **1 hour, 4 hours, 1 day, 1 week, 1 month** | 180 days |

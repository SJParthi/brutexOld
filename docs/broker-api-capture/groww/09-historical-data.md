# Groww Trading API — Historical Data (deprecated endpoint)

> Sources:
> - https://groww.in/trade-api/docs/curl/historical-data
> - https://groww.in/trade-api/docs/python-sdk/historical-data
> Captured: 2026-07-15

Fetch historical data of instruments easily using Groww APIs.

> **Note (verbatim from docs)**
>
> This API request is deprecated and will NOT work in the future. Use **Get Historical Candle Data** (Backtesting section) instead — see [10-backtesting.md](./10-backtesting.md).
>
> Python SDK: "This method is deprecated and will be removed in future releases. Please use `get_historical_candles` method instead."

## Get Historical Data (DEPRECATED)

`GET https://api.groww.in/v1/historical/candle/range`

This API can be used to get the historical data of an instrument for a given time range. It provides the historical candles for a given interval.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/historical/candle/range?exchange=NSE&segment=CASH&trading_symbol=WIPRO&start_time=2021-01-01 09:15:00&end_time=2021-01-01 15:15:00 \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

### Python SDK Usage (deprecated method `get_historical_candle_data`)

```python
from growwapi import GrowwAPI
import time

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

# you can give time programatically.
end_time_in_millis = int(time.time() * 1000) # epoch time in milliseconds
start_time_in_millis = end_time_in_millis - (24 * 60 * 60 * 1000) # last 24 hours

# OR

# you can give start time and end time in yyyy-MM-dd HH:mm:ss format.
end_time = "2025-02-27 14:00:00"
start_time = "2025-02-27 10:00:00"

historical_data_response = groww.get_historical_candle_data(
    trading_symbol="RELIANCE",
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    start_time=start_time,
    end_time=end_time,
    interval_in_minutes=5 # Optional: Interval in minutes for the candle data
)
print(historical_data_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO etc. |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| start_time `*` | string | Time in yyyy-MM-dd HH:mm:ss or epoch seconds format from which data is required (Python SDK docs say "epoch milliseconds") |
| end_time `*` | string | Time in yyyy-MM-dd HH:mm:ss or epoch seconds format till when data is required (Python SDK docs say "epoch milliseconds") |
| interval_in_minutes | string | Interval in minutes for which data is required |

`*` required parameters

### Response

All prices in rupees.

```json
{
  "status": "SUCCESS",
  "payload": {
    "candles": [
      [
        1633072800,
        150.1,
        155.0,
        145.0,
        152.4,
        10000
      ]
    ],
    "start_time": "2025-01-01 15:30:00",
    "end_time": "2025-01-01 15:30:00",
    "interval_in_minutes": 5
  }
}
```

Candle array order (documented inline): `[candle timestamp in epoch second, open price, high price, low price, close price, volume]`.

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| candles | array[array] | This contains the list of candles. Each candle has candle timestamp (epoch second), open (float), high (float), low (float), close (float), volume (int) in that order. |
| start_time | string | Start time in yyyy-MM-dd HH:mm:ss |
| end_time | string | End time in yyyy-MM-dd HH:mm:ss |
| interval_in_minutes | int | Interval in minutes |

### Interval limits (this deprecated endpoint)

| Candle Interval | Max Duration per Request | Historical Data Available |
| ---------------------- | ------------------------ | ------------------------- |
| **1 min** | 7 days | Last 3 months |
| **5 min** | 15 days | Last 3 months |
| **10 min** | 30 days | Last 3 months |
| **1 hour (60 min)** | 150 days | Last 3 months |
| **4 hours (240 min)** | 365 days | Last 3 months |
| **1 day (1440 min)** | 1080 days (~3 years) | Full history |
| **1 week (10080 min)** | No Limit | Full history |

For the current (non-deprecated) candles endpoint `GET /v1/historical/candles` with interval strings (`5minute`, `1day`, etc.), see [10-backtesting.md](./10-backtesting.md).

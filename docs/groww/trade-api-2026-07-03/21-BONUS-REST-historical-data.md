# Groww Trade API — REST API: Historical Data

> **Source:** https://groww.in/trade-api/docs/curl/historical-data
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Historical Data

Fetch historical data of instruments easily using Groww APIs

## [Get Historical Data](#get-historical-data)

> **Note**
>
> This API request is deprecated and will NOT work in the future. Use [Get Historical Candle Data](/trade-api/docs/curl/backtesting#get-historical-candle-data) instead.

`GET https://api.groww.in/v1/historical/candle/range`

This API can be used to get the historical data of an instrument for a given time range. It provides the historical candles for a given interval.

### [Request](#request)

```
# You can also use wget
curl -X GET https://api.groww.in/v1/historical/candle/range?exchange=NSE&segment=CASH&trading_symbol=WIPRO&start_time=2021-01-01 09:15:00&end_time=2021-01-01 15:15:00 \
  -H 'Accept: application/json' \
  -H 'Authorization: Bearer {ACCESS_TOKEN}' \
  -H 'X-API-VERSION: 1.0'
```

#### [Request schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/curl/annexures#exchange) |
| segment `*` | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO etc. |
| trading\_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |
| start\_time `*` | string | Time in yyyy-MM-dd HH:mm:ss or epoch seconds format from which data is required |
| end\_time `*` | string | Time in yyyy-MM-dd HH:mm:ss or epoch seconds format from which data is required |
| interval\_in\_minutes | string | Interval in minutes for which data is required |

`*`required parameters

### [Response](#response)

All prices in rupees.

```
{
  "status": "SUCCESS",
  "payload": {
    "candles": [
      [
        1633072800, // candle timestamp in epoch second
        150.1,  // open price
        155.0,  // high price
        145.0,  // low price
        152.4,  // close price
        10000   // volume
      ]
    ],
    "start_time": 2025-01-01 15:30:00,
    "end_time": 2025-01-01 15:30:00,
    "interval_in_minutes": 5
  }
}
```

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| candles | array[array] | This contains the list of candles. Each candle has candle timestamp (epoch second), open (float), high (float), low (float), close (float) , volume (int) in that order. |
| start\_time | string | Start time in yyyy-MM-dd HH:mm:ss |
| end\_time | string | End time in yyyy-MM-dd HH:mm:ss |
| interval\_in\_minutes | int | Interval in minutes |

| Candle Interval | Max Duration per Request | Historical Data Available |
| --- | --- | --- |
| **1 min** | 7 days | Last 3 months |
| **5 min** | 15 days | Last 3 months |
| **10 min** | 30 days | Last 3 months |
| **1 hour (60 min)** | 150 days | Last 3 months |
| **4 hours (240 min)** | 365 days | Last 3 months |
| **1 day (1440 min)** | 1080 days (~3 years) | Full history |
| **1 week (10080 min)** | No Limit | Full history |

[Previous

Live Data](/trade-api/docs/curl/live-data)[Next

Backtesting](/trade-api/docs/curl/backtesting)

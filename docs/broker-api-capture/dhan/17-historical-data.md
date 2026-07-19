# Historical Data (Charts)

> Source: https://dhanhq.co/docs/v2/historical-data/
> Captured: 2026-07-15

This API gives you historical candle data for the desired scrip across segments & exchange. This data is presented in the form of a candle and gives you timestamp, open, high, low, close & volume.

| Method | Endpoint           | Description                   |
| ------ | ------------------ | ----------------------------- |
| POST   | /charts/historical | Get OHLC for daily timeframe  |
| POST   | /charts/intraday   | Get OHLC for minute timeframe |

## Daily Historical Data

Retrieve OHLC & Volume of daily candle for desired instrument. The data for any scrip is available back upto the date of its inception.

```
curl --request POST \
--url https://api.dhan.co/v2/charts/historical \
--header 'Content-Type: application/json' \
--header 'access-token: JWT' \
--data '{}'
```

**Request Structure**

```
{
    "securityId": "1333",
    "exchangeSegment":"NSE_EQ",
    "instrument": "EQUITY",
    "expiryCode": 0,
    "oi": false,
    "fromDate": "2022-01-08",
    "toDate": "2022-02-08"
}
```

**Parameters**

| Field                        | Field Type   | Description                                                                                      |
| ---------------------------- | ------------ | -------------------------------------------------------------------------------------------------- |
| securityId   *required*      | string       | Exchange standard ID for each scrip. Refer [Instrument List](./21-instruments-scrip-master.md)     |
| exchangeSegment   *required* | enum string  | Exchange & segment for which data is to be fetched - [Annexure](./18-annexure.md#exchange-segment) |
| instrument      *required*   | enum string  | Instrument type of the scrip. Refer [Annexure → Instrument](./18-annexure.md#instrument)           |
| expiryCode      *optional*   | enum integer | Expiry of the instruments in case of derivatives. Refer [Instrument List](./21-instruments-scrip-master.md) |
| oi    *optional*             | boolean      | Open Interest data for Futures & Options                                                           |
| fromDate    *required*       | string       | Start date of the desired range                                                                    |
| toDate    *required*         | string       | End date of the desired range (non-inclusive)                                                      |

**Response Structure**

```
{
    "open": [
            3978,3856,3925,3918,3877.85,3992.7,4033.95,4012,3910,3807,3840,3769.5,3731,3646,3749,
            3770,3827.9,3851,3815.3,3791
        ],
    "high": [
        3978,3925,3929,3923,3977,4043,4041.7,4012,3920,3851.55,3849.65,3809.4,3733.4,3729.8,
        3758,3808,3864,3882.5,3824.7,3831.8
        ],
    "low":  [
        3861,3856,3836.55,3857,3860.05,3962.3,3980,3910.5,3811,3771.1,3740.1,3722.2,3625.1,
        3646,3721.4,3736.4,3800.65,3816.05,3769,3756.15
        ],
    "close": [
        3879.85,3915.9,3859.9,3897.9,3968.15,4019.15,3990.6,3914.65,3826.55,3833.5,3771.35,
        3769.9,3649.25,3690.05,3736.25,3800.65,3856.2,3824.6,3814.9,3779
        ],
    "volume":[
        3937092,1906106,3203744,6684507,3348123,3442604,2389041,3102539,6176776,3112358,  
        3258414,3330501,5718297,3143862,2739393,2105169,1984212,1960538,2307366,1919149
        ],
    "timestamp": [
        1326220200,1326306600,1326393000,1326479400,1326565800,1326825000,1326911400,
        1326997800,1327084200,1327170600,1327429800,1327516200,1327689000,1327775400,
        1328034600,1328121000,1328207400,1328293800,1328380200,1328639400
        ],
    "open_interest": [
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
}
```

**Parameters**

| Field     | Field Type | Description                    |
| --------- | ---------- | ------------------------------ |
| open      | float      | Open price of the timeframe    |
| high      | float      | High price in the timeframe    |
| low       | float      | Low price in the timeframe     |
| close     | float      | Close price of the timeframe   |
| volume    | int        | Volume traded in the timeframe |
| timestamp | int        | Epoch timestamp                |

(Response also includes `open_interest` array when `oi` is requested.)

## Intraday Historical Data

Retrieve Open, High, Low, Close, OI & Volume of 1, 5, 15, 25 and 60 min candle for desired instrument for last 5 years. This data available for all exchanges and segments for all active instruments.

```
curl --request POST \
--url https://api.dhan.co/v2/charts/intraday \
--header 'Accept: application/json' \
--header 'Content-Type: application/json' \
--header 'access-token: ' \
--data '{}'
```

**Request Structure**

```
{
"securityId": "1333",
"exchangeSegment": "NSE_EQ",
"instrument": "EQUITY",
"interval": "1",
"oi": false,
"fromDate": "2024-09-11 09:30:00",
"toDate": "2024-09-15 13:00:00"
}
```

**Parameters**

| Field                        | Field Type   | Description                                                                                      |
| ---------------------------- | ------------ | -------------------------------------------------------------------------------------------------- |
| securityId   *required*      | string       | Exchange standard ID for each scrip. Refer [Instrument List](./21-instruments-scrip-master.md)     |
| exchangeSegment   *required* | enum string  | Exchange & segment for which data is to be fetched - [Annexure](./18-annexure.md#exchange-segment) |
| instrument      *required*   | enum string  | Instrument type of the scrip. Refer [Annexure → Instrument](./18-annexure.md#instrument)           |
| interval      *required*     | enum integer | Minute intervals in timeframe    `1`, `5`, `15`, `25`, `60`                                        |
| oi    *optional*             | boolean      | Open Interest data for Futures & Options                                                           |
| fromDate    *required*       | string       | Start date of the desired range                                                                    |
| toDate    *required*         | string       | End date of the desired range                                                                      |

> **Note**: The data size is very large in this scenario and only 90 days of data can be polled at once for any of the above time intervals. It is recommended that you store this data at your end for day-to-day analysis.

**Response Structure** (arrays of equal length; sample truncated to representative values — structure identical to daily response with per-minute candles)

```
{
    "open":   [3750,3757.85,3751.2,3763.6,3759.55, ...],
    "high":   [3750,3757.9,3763.6,3765.2,3763.15, ...],
    "low":    [3750,3746.1,3749.25,3757,3758.65, ...],
    "close":  [3750,3751.25,3763.6,3760.85,3759, ...],
    "volume": [166,53629,34592,20802,11262, ...],
    "timestamp": [1328845020,1328845500,1328845560,1328845620,1328845680, ...],
    "open_interest": [0,0,0,0,0, ...]
}
```

**Parameters**

| Field     | Field Type | Description                    |
| --------- | ---------- | ------------------------------ |
| open      | float      | Open price of the timeframe    |
| high      | float      | High price in the timeframe    |
| low       | float      | Low price in the timeframe     |
| close     | float      | Close price of the timeframe   |
| volume    | int        | Volume traded in the timeframe |
| timestamp | int        | Epoch timestamp                |

Note: For description of enum values, refer [Annexure](./18-annexure.md)

---
Copyright © 2024 Moneylicious Securities Private Limited

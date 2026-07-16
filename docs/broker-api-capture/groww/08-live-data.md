# Groww Trading API — Live Data (Quote, LTP, OHLC, Option Chain, Greeks)

> Sources:
> - https://groww.in/trade-api/docs/curl/live-data
> - https://groww.in/trade-api/docs/python-sdk/live-data
> Captured: 2026-07-15

Fetch live data of instruments easily using APIs.

Endpoints summary:

| Operation | Method + Path | Python SDK method |
| --- | --- | --- |
| Get Quote | `GET /v1/live-data/quote?exchange=&segment=&trading_symbol=` | `groww.get_quote(...)` |
| Get LTP | `GET /v1/live-data/ltp?segment=&exchange_symbols=` | `groww.get_ltp(...)` |
| Get OHLC | `GET /v1/live-data/ohlc?segment=&exchange_symbols=` | `groww.get_ohlc(...)` |
| Get Option Chain | `GET /v1/option-chain/exchange/{exchange}/underlying/{underlying}?expiry_date={expiry_date}` | `groww.get_option_chain(...)` |
| Get Greeks | `GET /v1/live-data/greeks/exchange/{exchange}/underlying/{underlying}/trading_symbol/{trading_symbol}/expiry/{expiry}` | `groww.get_greeks(...)` |

All prices in rupees.

---

## Get Quote

`GET https://api.groww.in/v1/live-data/quote`

This API provides the complete live data snapshot for an instrument including the latest price, market depth, ohlc, market volumes and much more. If one requires only the latest price data then the Get LTP api should be used. Similarly if one is interested in getting only ohlc then Get OHLC api should be used. Use the segment value FNO for derivatives, CASH for stocks and index, and COMMODITY for commodity contracts.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=NIFTY \
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

quote_response = groww.get_quote(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    trading_symbol="NIFTY"
)
print(quote_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| trading_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "average_price": 150.25,
    "bid_quantity": 1000,
    "bid_price": 150,
    "day_change": -0.5,
    "day_change_perc": -0.33,
    "upper_circuit_limit": 151,
    "lower_circuit_limit": 148.5,
    "ohlc": {
      "open": 149.50,
      "high": 150.50,
      "low": 148.50,
      "close": 149.50
    },
    "depth": {
      "buy": [
        {"price": 100.5, "quantity": 1000}
      ],
      "sell": [
        {"price": 100.5, "quantity": 1000}
      ]
    },
    "high_trade_range": 150.5,
    "implied_volatility": 0.25,
    "last_trade_quantity": 500,
    "last_trade_time": 1633072800000,
    "low_trade_range": 148.25,
    "last_price": 149.5,
    "market_cap": 5000000000,
    "offer_price": 150.5,
    "offer_quantity": 2000,
    "oi_day_change": 100,
    "oi_day_change_percentage": 0.5,
    "open_interest": 2000,
    "previous_open_interest": 1900,
    "total_buy_quantity": 5000,
    "total_sell_quantity": 4000,
    "volume": 10000,
    "week_52_high": 160,
    "week_52_low": 140
  }
}
```

(Note: the cURL docs render `ohlc` as the string `"{open: 149.50,high: 150.50,low: 148.50,close: 149.50}"`; the Python SDK docs show it as a proper nested object as above.)

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| average_price | decimal/float | Average price of the instrument in Rupees |
| bid_quantity | integer | Quantity of the bid |
| bid_price | decimal/float | Price of the bid |
| day_change | decimal/float | Day change in price |
| day_change_perc | decimal/float | Day change percentage |
| upper_circuit_limit | decimal/float | High price range |
| lower_circuit_limit | decimal/float | Low price range |
| ohlc.open | decimal/float | Opening price |
| ohlc.high | decimal/float | Highest price |
| ohlc.low | decimal/float | Lowest price |
| ohlc.close | decimal/float | Closing price |
| depth.buy[]/sell[].price | decimal/float | Price of the book entry |
| depth.buy[]/sell[].quantity | integer | Quantity of the book entry |
| high_trade_range | decimal/float | High trade range |
| implied_volatility | decimal/float | Implied volatility |
| last_trade_quantity | integer | Last trade quantity |
| last_trade_time | integer | Last trade time in epoch milliseconds |
| low_trade_range | decimal/float | Low trade range |
| last_price | decimal/float | Last traded price |
| market_cap | decimal/float | Market capitalization |
| offer_price | decimal/float | Offer price |
| offer_quantity | integer | Quantity of the offer |
| oi_day_change | decimal/float | Open interest day change |
| oi_day_change_percentage | decimal/float | Open interest day change percentage |
| open_interest | decimal/float | Open interest |
| previous_open_interest | decimal/float | Previous open interest |
| total_buy_quantity | decimal/float | Total buy quantity |
| total_sell_quantity | decimal/float | Total sell quantity |
| volume | integer | Volume of trades |
| week_52_high | decimal/float | 52-week high price |
| week_52_low | decimal/float | 52-week low price |

---

## Get LTP

`GET https://api.groww.in/v1/live-data/ltp`

The API can be used to get the latest price of an instrument. Use the segment value FNO for derivatives, CASH for stocks and indices, and COMMODITY for commodity contracts. **Upto 50 instruments are supported for each api call.**

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/live-data/ltp?segment=CASH&exchange_symbols=NSE_RELIANCE,BSE_SENSEX \
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

ltp_response = groww.get_ltp(
  segment=groww.SEGMENT_CASH,
  exchange_trading_symbols="NSE_NIFTY"
)
print(ltp_response)

# you can pass multiple instruments at once
multiple_ltp_response = groww.get_ltp(
  segment=groww.SEGMENT_CASH,
  exchange_trading_symbols=("NSE_NIFTY", "NSE_RELIANCE")
)
print(multiple_ltp_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| exchange_symbols `*` (REST) / exchange_trading_symbols `*` (SDK) | array[string] / Tuple[str] | String of trading symbols with their respective exchanges. For example `NSE_RELIANCE` `BSE_SENSEX` `NSE_NIFTY25APR24100PE` `MCX_CRUDEOIL25JAN25FUT` |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "NSE_RELIANCE": 2334.2,
    "BSE_SENSEX": 73243.4
  }
}
```

Python SDK example (payload only):

```json
{
  "NSE_RELIANCE": 2500.5,
  "NSE_NIFTY": 22962.10
}
```

Response schema:

| Name | Type | Description |
| ------ | ------- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| `{EXCHANGE_SYMBOL}` (ltp) | decimal/float | Last traded price keyed by exchange_symbol |

---

## Get OHLC

`GET https://api.groww.in/v1/live-data/ohlc`

The API can be used to get the ohlc data of an instrument. Use the segment value FNO for derivatives, CASH for stocks and indices, and COMMODITY for commodity contracts. **Upto 50 instruments are supported for each API call.**

Note: The OHLC data retrieved using the OHLC API reflects the current time's OHLC (i.e., real-time snapshot). For interval-based OHLC data (e.g., 1-minute, 5-minute candles), please refer to the [Historical Data](./09-historical-data.md) API.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/live-data/ohlc?segment=CASH&exchange_symbols=NSE_RELIANCE,BSE_SENSEX \
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

ohlc_response = groww.get_ohlc(
  segment=groww.SEGMENT_CASH,
  exchange_trading_symbols="NSE_NIFTY"
)
print(ohlc_response)

# you can pass multiple instruments at once
multiple_ohlc_response = groww.get_ohlc(
  segment=groww.SEGMENT_CASH,
  exchange_trading_symbols=("NSE_NIFTY", "NSE_RELIANCE")
)
print(multiple_ohlc_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | [Segment](./13-annexures.md#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| exchange_symbols `*` (REST) / exchange_trading_symbols `*` (SDK) | array[string] / Tuple[str] | String of trading symbols with their respective exchanges. For example `NSE_RELIANCE` `BSE_SENSEX` `NSE_NIFTY25APR24100PE` `MCX_CRUDEOIL25JANFUT` |

`*` required parameters

### Response

REST sample:

```json
{
  "status": "SUCCESS",
  "payload": {
    "NSE_RELIANCE": "{open: 149.50,high: 150.50,low: 148.50,close: 149.50}",
    "BSE_SENSEX": "{open: 149.50,high: 150.50,low: 148.50,close: 149.50}"
  }
}
```

Python SDK sample (payload only, structured):

```json
{
  "NSE_NIFTY": {
    "open": 22516.45,
    "high": 22613.3,
    "low": 22526.4,
    "close": 22547.55
  },
  "NSE_RELIANCE": {
    "open": 1212.8,
    "high": 1215.0,
    "low": 1201.0,
    "close": 1204.0
  }
}
```

Response schema:

| Name | Type | Description |
| ------ | ------- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| open | decimal/float | Opening price |
| high | decimal/float | Highest price |
| low | decimal/float | Lowest price |
| close | decimal/float | Closing price |

---

## Get Option Chain

`GET https://api.groww.in/v1/option-chain/exchange/{exchange}/underlying/{underlying}?expiry_date={expiry_date}`

This API provides the complete option chain data for FNO (Futures and Options) contracts including Greeks. Option chains are lists of available contracts for a specific underlying symbol and expiry date. This API is specifically designed for derivatives trading. Provides comprehensive data including Greeks, LTP, open interest, and volume for all available strikes for both Call (CE) and Put (PE) options.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/option-chain/exchange/{exchange}/underlying/{underlying}?expiry_date={expiry_date} \
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

# Get option chain for NIFTY with specific expiry date
option_chain_response = groww.get_option_chain(
    exchange=groww.EXCHANGE_NSE,
    underlying="NIFTY",
    expiry_date="2025-11-28"
)
print(option_chain_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) - NSE or BSE |
| underlying `*` | string | Underlying symbol for the contract such as NIFTY, BANKNIFTY, RELIANCE etc. |
| expiry_date `*` | string | Expiry date of the contract in YYYY-MM-DD format. |

`*` required parameters

### Response

```json
{
    "status": "SUCCESS",
    "payload": {
        "underlying_ltp": 25641.7,
        "strikes": {
            "23400": {
                "CE": {
                    "greeks": {
                        "delta": 0.9936,
                        "gamma": 0,
                        "theta": -1.0787,
                        "vega": 0.6943,
                        "rho": 5.1802,
                        "iv": 25.3409
                    },
                    "trading_symbol": "NIFTY25N1823400CE",
                    "ltp": 2200,
                    "open_interest": 7,
                    "volume": 5
                },
                "PE": {
                    "greeks": {
                        "delta": -0.0064,
                        "gamma": 0,
                        "theta": -1.0787,
                        "vega": 0.6943,
                        "rho": -0.0373,
                        "iv": 25.3409
                    },
                    "trading_symbol": "NIFTY25N1823400PE",
                    "ltp": 2.05,
                    "open_interest": 7453,
                    "volume": 9339
                }
            },
            "23450": {
                "CE": {
                    "greeks": {
                        "delta": 0.9927,
                        "gamma": 0,
                        "theta": -1.2027,
                        "vega": 0.7774,
                        "rho": 5.1862,
                        "iv": 25.2306
                    },
                    "trading_symbol": "NIFTY25N1823450CE",
                    "ltp": 2082.9,
                    "open_interest": 4,
                    "volume": 0
                },
                "PE": {
                    "greeks": {
                        "delta": -0.0073,
                        "gamma": 0,
                        "theta": -1.2027,
                        "vega": 0.7774,
                        "rho": -0.0424,
                        "iv": 25.2306
                    },
                    "trading_symbol": "NIFTY25N1823450PE",
                    "ltp": 2.35,
                    "open_interest": 378,
                    "volume": 74
                }
            }
        }
    }
}
```

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| underlying_ltp | decimal/float | Last Traded Price of the underlying |
| strikes | object | Strike-wise option chain data containing CE and PE contracts |
| strikes.{strike}.CE/PE.trading_symbol | string | Trading symbol of the option contract |
| strikes.{strike}.CE/PE.ltp | float | Last traded price of the option contract |
| strikes.{strike}.CE/PE.open_interest | int | Open interest for the contract |
| strikes.{strike}.CE/PE.volume | int | Total volume traded for the contract |
| greeks.delta | float | Delta measures the rate of change of option price based on every 1 rupee change in the price of underlying |
| greeks.gamma | float | Gamma measures the rate of change of delta with respect to underlying asset price |
| greeks.theta | float | Theta measures the rate of time decay of option price |
| greeks.vega | float | Vega measures the rate of change of option price based on every 1% change in implied volatility |
| greeks.rho | float | Rho measures the sensitivity of option price to changes in interest rates |
| greeks.iv | float | Implied Volatility represents the market's expectation of future volatility, expressed as a percentage |

---

## Get Greeks

`GET https://api.groww.in/v1/live-data/greeks/exchange/{exchange}/underlying/{underlying}/trading_symbol/{trading_symbol}/expiry/{expiry}`

This API provides the complete Greeks data for FNO (Futures and Options) contracts. Greeks are financial measures that help assess the risk and sensitivity of options contracts to various factors like underlying price changes, time decay, volatility, and interest rates. This API is specifically designed for derivatives trading and risk management.

### cURL Request

```bash
# You can also use wget
curl -X GET https://api.groww.in/v1/live-data/greeks/exchange/NSE/underlying/NIFTY/trading_symbol/NIFTY25O1425100CE/expiry/2025-10-14 \
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

greeks_response = groww.get_greeks(
    exchange=groww.EXCHANGE_NSE,
    underlying="NIFTY",
    trading_symbol="NIFTY25O1425100CE",
    expiry="2025-10-14"
)
print(greeks_response)
```

### Request schema

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](./13-annexures.md#exchange) - NSE or BSE |
| underlying `*` | string | Underlying symbol for the contract such as NIFTY, BANKNIFTY, RELIANCE etc. |
| trading_symbol `*` | string | Trading Symbol of the FNO contract as defined by the exchange |
| expiry `*` | string | Expiry date of the contract in YYYY-MM-DD format |

`*` required parameters

### Response

```json
{
  "status": "SUCCESS",
  "payload": {
    "greeks": {
      "delta": 0.6006,
      "gamma": 0.0014,
      "theta": -8.1073,
      "vega": 13.1433,
      "rho": 2.7333,
      "iv": 8.2383
    }
  }
}
```

### Response Schema

| Name | Type | Description |
| --- | --- | --- |
| status | string | SUCCESS if request is processed successfully, FAILURE if the request failed (REST wrapper only) |
| delta | float | Delta measures the rate of change of option price based on every 1 rupee change in the price of underlying. |
| gamma | float | Gamma measures the rate of change of delta with respect to underlying asset price. Higher gamma means delta changes more rapidly |
| theta | float | Theta measures the rate of time decay of option price. Usually negative, indicating option value decreases over time |
| vega | float | Vega measures the rate of change of option price based on every 1% change in implied volatility of the underlying asset |
| rho | float | Rho measures the sensitivity of option price to changes in interest rates |
| iv | float | Implied Volatility represents the market's expectation of future volatility, expressed as a percentage |

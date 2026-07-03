# Groww Trade API — Live Data — REST (Quote / LTP / OHLC)

> **Source:** https://groww.in/trade-api/docs/python-sdk/live-data
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Live Data

This guide describes how to fetch live data of instruments easily using the SDK.

## [Get Quote](#get-quote)

Fetch a real-time quote for an individual instrument using this `get_quote` method.

### [Python SDK Usage](#python-sdk-usage)

```
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

#### [Request Schema](#request-schema)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) |
| segment `*` | string | [Segment](/trade-api/docs/python-sdk/annexures#segment) of the instrument such as CASH, FNO etc. |
| trading\_symbol `*` | string | Trading Symbol of the instrument as defined by the exchange |

`*`required parameters

### [Response Payload](#response-payload)

All prices in rupees.

```
{
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
```

#### [Response Schema](#response-schema)

| Name | Type | Description |
| --- | --- | --- |
| average\_price | float | Average price of the instrument |
| bid\_quantity | int | Quantity of the bid |
| bid\_price | float | Price of the bid |
| day\_change | float | Day change in price |
| day\_change\_perc | float | Day change percentage |
| upper\_circuit\_limit | float | High price range |
| lower\_circuit\_limit | float | Low price range |
| open | float | Opening price |
| high | float | Highest price |
| low | float | Lowest price |
| close | float | Closing price |
| price | float | Price of the book entry |
| quantity | int | Quantity of the book entry |
| high\_trade\_range | float | High trade range |
| implied\_volatility | float | Implied volatility |
| last\_trade\_quantity | int | Last trade quantity |
| last\_trade\_time | int | Last trade time in epoch milliseconds |
| low\_trade\_range | float | Low trade range |
| last\_price | float | Last traded price |
| market\_cap | float | Market capitalization |
| offer\_price | float | Offer price |
| offer\_quantity | int | Quantity of the offer |
| oi\_day\_change | float | Open interest day change |
| oi\_day\_change\_percentage | float | Open interest day change percentage |
| open\_interest | float | Open interest |
| previous\_open\_interest | float | Previous open interest |
| total\_buy\_quantity | float | Total buy quantity |
| total\_sell\_quantity | float | Total sell quantity |
| volume | int | Volume of trades |
| week\_52\_high | float | 52-week high price |
| week\_52\_low | float | 52-week low price |

## [Get LTP](#get-ltp)

Fetch the last traded price for multiple instruments using this `get_ltp` method.

Upto 50 instruments are supported for each function call.

### [Python SDK Usage](#python-sdk-usage-1)

```
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

#### [Request Schema](#request-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | [Segment](/trade-api/docs/python-sdk/annexures#segment) of the instrument such as CASH, FNO etc. |
| exchange\_trading\_symbols `*` | Tuple[str] | String of trading symbols with their respective exchanges |

`*`required parameters

### [Response Payload](#response-payload-1)

All prices in rupees.

```
{
  "NSE_RELIANCE": 2500.5,
  "NSE_NIFTY": 22962.10
}
```

#### [Response Schema](#response-schema-1)

| Name | Type | Description |
| --- | --- | --- |
| ltp | float | Last traded price |

## [Get OHLC](#get-ohlc)

Get the OHLC details for list of given instruments quickly using this `get_ohlc` method.
Use the segment value FNO for derivatives and CASH for stocks and index.

Upto 50 instruments are supported for each function call.

Note: The OHLC data retrieved using the OHLC SDK method reflects the current time's OHLC (i.e., real-time snapshot).
For interval-based OHLC data (e.g., 1-minute, 5-minute candles), please refer to the [Historical Data](/trade-api/docs/python-sdk/historical-data) SDK method.

### [Python SDK Usage](#python-sdk-usage-2)

```
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

#### [Request Schema](#request-schema-2)

| Name | Type | Description |
| --- | --- | --- |
| segment `*` | string | [Segment](/trade-api/docs/python-sdk/annexures#segment) of the instrument such as CASH, FNO etc. |
| exchange\_trading\_symbols `*` | Tuple[str] | String of trading symbols with their respective exchanges |

`*`required parameters

### [Response Payload](#response-payload-2)

All prices in rupees.

```
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

#### [Response Schema](#response-schema-2)

| Name | Type | Description |
| --- | --- | --- |
| open | float | Opening price |
| high | float | Highest price |
| low | float | Lowest price |
| close | float | Closing price |

## [Get Option Chain](#get-option-chain)

Fetch the complete option chain data for FNO (Futures and Options) contracts including Greeks using this `get_option_chain` method.

Option chains are lists of available contracts for a specific underlying symbol and expiry date. This method provides comprehensive data including Greeks, LTP, open interest, and volume for all available strikes for both Call (CE) and Put (PE) options.

### [Python SDK Usage](#python-sdk-usage-3)

```
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

#### [Request Schema](#request-schema-3)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) - NSE or BSE |
| underlying `*` | string | Underlying symbol for the contract such as NIFTY, BANKNIFTY, RELIANCE etc. |
| expiry\_date`*` | string | Expiry date of the contract in YYYY-MM-DD format. |

`*`required parameters

### [Response Payload](#response-payload-3)

All prices in rupees.

```
{
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
```

#### [Response Schema](#response-schema-3)

| Name | Type | Description |
| --- | --- | --- |
| underlying\_ltp | float | Last traded price of the underlying instrument |
| strikes | object | Strike-wise option chain data containing CE and PE contracts |
| trading\_symbol | string | Trading symbol of the option contract |
| ltp | float | Last traded price of the option contract |
| open\_interest | int | Open interest for the contract |
| volume | int | Total volume traded for the contract |
| delta | float | Delta measures the rate of change of option price based on every 1 rupee change in the price of underlying |
| gamma | float | Gamma measures the rate of change of delta with respect to underlying asset price |
| theta | float | Theta measures the rate of time decay of option price |
| vega | float | Vega measures the rate of change of option price based on every 1% change in implied volatility |
| rho | float | Rho measures the sensitivity of option price to changes in interest rates |
| iv | float | Implied Volatility represents the market's expectation of future volatility, expressed as a percentage |

## [Get Greeks](#get-greeks)

Fetch the Greeks data for FNO (Futures and Options) contracts using this `get_greeks` method.

Greeks are financial measures that help assess the risk and sensitivity of options contracts to various factors like underlying price changes, time decay, volatility, and interest rates. This method is specifically designed for derivatives trading and risk management.

### [Python SDK Usage](#python-sdk-usage-4)

```
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

#### [Request Schema](#request-schema-4)

| Name | Type | Description |
| --- | --- | --- |
| exchange `*` | string | [Stock exchange](/trade-api/docs/python-sdk/annexures#exchange) - NSE or BSE |
| underlying `*` | string | Underlying symbol for the contract such as NIFTY, BANKNIFTY, RELIANCE etc. |
| trading\_symbol `*` | string | Trading Symbol of the FNO contract as defined by the exchange |
| expiry `*` | string | Expiry date of the contract in YYYY-MM-DD format |

`*`required parameters

### [Response Payload](#response-payload-4)

```
{
  "greeks": {
    "delta": 0.6006,
    "gamma": 0.0014,
    "theta": -8.1073,
    "vega": 13.1433,
    "rho": 2.7333,
    "iv": 8.2383
  }
}
```

#### [Response Schema](#response-schema-4)

| Name | Type | Description |
| --- | --- | --- |
| delta | float | Delta measures the rate of change of option price based on every 1 rupee change in the price of underlying. |
| gamma | float | Gamma measures the rate of change of delta with respect to underlying asset price. Higher gamma means delta changes more rapidly |
| theta | float | Theta measures the rate of time decay of option price. Usually negative, indicating option value decreases over time |
| vega | float | Vega measures the rate of change of option price based on every 1% change in implied volatility of the underlying asset |
| rho | float | Rho measures the sensitivity of option price to changes in interest rates |
| iv | float | Implied Volatility represents the market's expectation of future volatility, expressed as a percentage |

[Previous

Margin](/trade-api/docs/python-sdk/margin)[Next

Historical Data](/trade-api/docs/python-sdk/historical-data)

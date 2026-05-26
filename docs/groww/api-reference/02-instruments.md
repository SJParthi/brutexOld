---
source_url: https://groww.in/trade-api/docs/python-sdk/instruments
fetched_at: 2026-05-26
api: Groww Trading API Python SDK
section: Instruments
---

# Instruments

This guide explains how to download and use instrument data with the Groww API SDK.

## Overview

Instruments are the financial assets that can be traded on exchanges through the Groww API. These include stocks, futures, options, commodities, indices and other tradable securities. The Instruments API provides essential data about these trading instruments needed for many operations.

**Full CSV download URL:** https://growwapi-assets.groww.in/instruments/instrument.csv

## CSV Format

The full CSV file contains instrument details with columns documented below. Sample rows:

```
      exchange exchange_token         trading_symbol                    groww_symbol name instrument_type segment series isin underlying_symbol underlying_exchange_token expiry_date strike_price lot_size tick_size freeze_quantity is_reserved buy_allowed sell_allowed
10000      NSE          49445  BANKNIFTY25DEC27000PE  NSE-BANKNIFTY-24Dec25-27000-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        27000       35      0.05             601           1           1            1
10001      NSE          60123  BANKNIFTY25DEC51000CE  NSE-BANKNIFTY-24Dec25-51000-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        51000       35      0.05             601           0           1            1
10002      NSE          60103  BANKNIFTY25DEC40500CE  NSE-BANKNIFTY-24Dec25-40500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        40500       35      0.05             601           0           1            1
10003      NSE          42279  BANKNIFTY25SEP28500CE  NSE-BANKNIFTY-25Sep25-28500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-09-25        28500       35      0.05             601           1           1            1
10004      NSE          61719  BANKNIFTY25SEP39000CE  NSE-BANKNIFTY-25Sep25-39000-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-09-25        39000       35      0.05             601           1           1            1
10005      NSE          60099  BANKNIFTY25DEC37500CE  NSE-BANKNIFTY-24Dec25-37500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        37500       35      0.05             601           1           1            1
10006      NSE          41942  BANKNIFTY25JUN35400PE  NSE-BANKNIFTY-26Jun25-35400-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-06-26        35400       30      0.05             601           1           1            1
```

> All prices in rupees.

## Python SDK Usage

### Get instrument by Groww symbol

```python
from growwapi import GrowwAPI

# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "your_token"

# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)

get_instrument_by_groww_symbol_response = groww.get_instrument_by_groww_symbol(
    groww_symbol="NSE-RELIANCE"
)
print(get_instrument_by_groww_symbol_response)
```

**Output:**

```python
Instrument: {'exchange': 'NSE', 'exchange_token': 2885, 'trading_symbol': 'RELIANCE',
'groww_symbol': 'NSE-RELIANCE', 'name': 'Reliance Industries', 'instrument_type': 'EQ',
'segment': 'CASH', 'series': 'EQ', 'isin': 'INE002A01018', 'underlying_symbol': nan,
'underlying_exchange_token': nan, 'lot_size': 1, 'expiry_date': nan, 'strike_price': nan,
'tick_size': 5, 'freeze_quantity': nan, 'is_reserved': 0, 'buy_allowed': 1, 'sell_allowed': 1}
```

### Get instrument by trading symbol

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

get_instrument_by_trading_symbol_response = groww.get_instrument_by_exchange_and_trading_symbol(
    exchange=groww.EXCHANGE_NSE,
    trading_symbol="RELIANCE"
)
print(get_instrument_by_trading_symbol_response)
```

### Get instrument by exchange token

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

get_instrument_by_exchange_token_response = groww.get_instrument_by_exchange_token(
    exchange_token="2885"
)
print(get_instrument_by_exchange_token_response)
```

### Get all instruments as a DataFrame

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

instruments_df = groww.get_all_instruments()
print(instruments_df.head())
```

**Output:**

```
  exchange exchange_token          trading_symbol                   groww_symbol  ... is_reserved buy_allowed sell_allowed internal_trading_symbol
0      NSE          35154      ASIANPAINT25FEBFUT       NSE-ASIANPAINT-Feb25-FUT  ...           0           1            1      ASIANPAINT25FEBFUT
1      NSE          35158          ABB25APR9600PE          NSE-ABB-Apr25-9600-PE  ...           1           1            1          ABB25APR9600PE
2      NSE          35044       NIFTY25MAR29050PE     NSE-NIFTY-27Mar25-29050-PE  ...           1           1            1       NIFTY25MAR29050PE
3      NSE          35093    FINNIFTY25MAR29600PE    NSE-FINNIFTY-Mar25-29600-PE  ...           1           1            1    FINNIFTY25MAR29600PE
4      NSE          35047  NIFTYNXT5025FEB56600CE  NSE-NIFTYNXT50-Feb25-56600-CE  ...           0           1            1  NIFTYNXT5025FEB56600CE
```

## Using CSV Data

Create a structured model from the CSV data and use it for various purposes in the SDK.

### Combined example: fetch live data using an instrument

```python
import pandas as pd
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

# Select an instrument (example: Reliance Industries)
selected = groww.get_instrument_by_groww_symbol("NSE-RELIANCE")

# Fetch latest price data for the selected instrument
ltp_response = groww.get_ltp(
    exchange_trading_symbols="NSE_" + selected['trading_symbol'],
    segment=selected['segment'],
)
print("Live Data:", ltp_response)
```

**Output:**

```python
{"ltp" : 149.5}
```

## Instrument CSV Columns

| Name | Type | Description |
|---|---|---|
| exchange | string | The Exchange where the instrument is traded |
| exchange_token | string | The unique token assigned to the instrument by the exchange |
| trading_symbol | string | The trading symbol of the instrument to place orders with |
| groww_symbol | string | The symbol used by Groww to identify the instrument |
| name | string | The name of the instrument |
| instrument_type | string | The type of the instrument (EQ, IDX, FUT, CE, PE) |
| segment | string | Segment of the instrument such as CASH, FNO, etc. |
| series | string | The series of the instrument (e.g., EQ, A, B, etc.) |
| isin | string | The ISIN (International Securities Identification Number) of the instrument |
| underlying_symbol | string | The symbol of the underlying asset (for derivatives). Empty for stocks and indices |
| underlying_exchange_token | string | The exchange token of the underlying asset |
| lot_size | number | The minimum lot size for trading the instrument |
| expiry_date | string | The expiry date of the instrument (for Derivatives) |
| strike_price | number | The strike price of the instrument (for Options) |
| tick_size | number | The minimum price movement for the instrument |
| freeze_quantity | number | The quantity that is frozen for trading |
| is_reserved | boolean | Whether the instrument is reserved for trading |
| buy_allowed | boolean | Whether buying the instrument is allowed |
| sell_allowed | boolean | Whether selling the instrument is allowed |

## Advanced Uses

You can also download the CSV file and use it for your own purposes:

```python
from growwapi import GrowwAPI
import pandas as pd

# Load the CSV file
instrument_df = pd.read_csv(groww.INSTRUMENT_CSV_URL)  # groww.INSTRUMENT_CSV_URL points to the CSV URL

# OR you can also do this
instrument_df = groww._load_instruments()
print(instrument_df.head())
```

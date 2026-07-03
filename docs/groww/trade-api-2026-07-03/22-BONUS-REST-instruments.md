# Groww Trade API — REST API: Instruments

> **Source:** https://groww.in/trade-api/docs/curl/instruments
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Instruments

This guide explains how to download and use instrument data with the Groww API.

Instruments are the financial assets that can be traded on exchanges through the Groww API. These include stocks, futures, options, indices, commodity derivatives and other tradable securities. The Instruments API provides essential data about these trading instruments that you'll need for many operations in the Groww API.

The CSV file includes instruments from multiple segments:

- **CASH**: Equity stocks and indices (NSE, BSE)
- **FNO**: Equity futures and options (NSE, BSE)
- **COMMODITY**: Commodity futures and options (MCX)

You can download the instruments csv file from [here](https://growwapi-assets.groww.in/instruments/instrument.csv).

## [Download Instrument CSV](#download-instrument-csv)

Use the `download instrument url` to retrieve a comma-separated CSV file with instrument details.

```
# You can also use wget
curl -X GET https://growwapi-assets.groww.in/instruments/instrument.csv
```

#### [CSV Format](#csv-format)

The full CSV file contains instrument details in the following format. Each individual column [is explained below](#instrument-csv-columns).

```out
      exchange exchange_token         trading_symbol                    groww_symbol name instrument_type segment series isin underlying_symbol underlying_exchange_token expiry_date strike_price lot_size tick_size freeze_quantity is_reserved buy_allowed sell_allowed
10000      NSE          49445  BANKNIFTY25DEC27000PE  NSE-BANKNIFTY-24Dec25-27000-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        27000       35      0.05             601           1           1            1
10001      NSE          60123  BANKNIFTY25DEC51000CE  NSE-BANKNIFTY-24Dec25-51000-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        51000       35      0.05             601           0           1            1
10002      NSE          60103  BANKNIFTY25DEC40500CE  NSE-BANKNIFTY-24Dec25-40500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        40500       35      0.05             601           0           1            1
10003      NSE          42279  BANKNIFTY25SEP28500CE  NSE-BANKNIFTY-25Sep25-28500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-09-25        28500       35      0.05             601           1           1            1
10004      NSE          61719  BANKNIFTY25SEP39000CE  NSE-BANKNIFTY-25Sep25-39000-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-09-25        39000       35      0.05             601           1           1            1
10005      NSE          60099  BANKNIFTY25DEC37500CE  NSE-BANKNIFTY-24Dec25-37500-CE  NaN              CE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-12-24        37500       35      0.05             601           1           1            1
10006      NSE          41942  BANKNIFTY25JUN35400PE  NSE-BANKNIFTY-26Jun25-35400-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-06-26        35400       30      0.05             601           1           1            1
10007      NSE          73176     BANKINDIA25JUL96CE     NSE-BANKINDIA-31Jul25-96-CE  NaN              CE     FNO    NaN  NaN         BANKINDIA                      4745  2025-07-31           96     5200      0.05          208001           0           1            1
10008      NSE          63148  BANKNIFTY25SEP70500PE  NSE-BANKNIFTY-25Sep25-70500-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-09-25        70500       35      0.05             601           1           1            1
10009      NSE          57317  BANKNIFTY25JUN37000PE  NSE-BANKNIFTY-26Jun25-37000-PE  NaN              PE     FNO    NaN  NaN         BANKNIFTY                     26009  2025-06-26        37000       30      0.05             601           1           1            1
...
```

All prices in rupees.

## [Instrument CSV Columns](#instrument-csv-columns)

The CSV contains the following columns (separated by commas):

| Name | Type | Description |
| --- | --- | --- |
| exchange | string | The [Exchange](/trade-api/docs/curl/annexures#exchange) where the instrument is traded |
| exchange\_token | string | The unique token assigned to the instrument by the exchange |
| trading\_symbol | string | The trading symbol of the instrument to place orders with |
| groww\_symbol | string | The symbol used by Groww to identify the instrument |
| name | string | The name of the instrument |
| instrument\_type | string | The [type of the instrument](/trade-api/docs/curl/annexures#instrument-type) |
| segment | string | [Segment](/trade-api/docs/curl/annexures#segment) of the instrument such as CASH, FNO, COMMODITY etc. |
| series | string | The series of the instrument (e.g., EQ, A, B, etc.) |
| isin | string | The ISIN (International Securities Identification number) of the instrument |
| underlying\_symbol | string | The symbol of the underlying asset (for derivatives). Empty for stocks and indices |
| underlying\_exchange\_token | string | The exchange token of the underlying asset |
| lot\_size | integer | The minimum lot size for trading the instrument |
| expiry\_date | string | The expiry date of the instrument (for Derivatives) |
| strike\_price | integer | The strike price of the instrument (for Options) |
| tick\_size | decimal | The minimum price movement for the instrument |
| freeze\_quantity | integer | The quantity that is frozen for trading |
| is\_reserved | boolean | Whether the instrument is reserved for trading |
| buy\_allowed | boolean | Whether buying the instrument is allowed |
| sell\_allowed | boolean | Whether selling the instrument is allowed |

[Previous

Introduction](/trade-api/docs/curl)[Next

Orders](/trade-api/docs/curl/orders)

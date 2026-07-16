# Instrument List (Scrip Master)

> Source: https://dhanhq.co/docs/v2/instruments/
> Captured: 2026-07-15

You can fetch instrument list for all instruments which can be traded via Dhan by using below URL:

**Compact:**

```
https://images.dhan.co/api-data/api-scrip-master.csv
```

**Detailed:**

```
https://images.dhan.co/api-data/api-scrip-master-detailed.csv
```

This fetches list of instruments as CSV with Security ID and other important details which will help you build with DhanHQ APIs.

### Segmentwise List

You can fetch detailed instrument list for all instruments in a particular exchange and segment by passing the same in parameters as below:

```
curl --location 'https://api.dhan.co/v2/instrument/{exchangeSegment}' \
```

> This helps to fetch instrument list of only one particular `exchangeSegment` at a time. The mapping of the same can be found in [Annexure → Exchange Segment](./18-annexure.md#exchange-segment).

### Column Description

| Detailed   `tag`                | Compact   `tag`            | Description                                                                                                            |
| ------------------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `EXCH_ID`                       | `SEM_EXM_EXCH_ID`          | Exchange `NSE` `BSE` `MCX`                                                                                             |
| `SEGMENT`                       | `SEM_SEGMENT`              | Segment   `C` - Currency   `D` - Derivatives   `E` - Equity   `M` - Commodity                                          |
| `ISIN`                          | -                          | International Securities Identification Number(ISIN) - 12-digit alphanumeric code unique for instruments               |
| `INSTRUMENT`                    | `SEM_INSTRUMENT_NAME`      | Instrument defined by Exchange - defined in [Annexure → Instrument](./18-annexure.md#instrument)                       |
| *removed*                       | `SEM_EXPIRY_CODE`          | Expiry Code (applicable in case of Futures Contract) - defined in [Annexure → Expiry Code](./18-annexure.md#expiry-code) |
| `UNDERLYING_SECURITY_ID`        | -                          | Security ID of underlying instrument (applicable in case of derivative contracts)                                      |
| `UNDERLYING_SYMBOL`             | -                          | Symbol of underlying instrument (applicable in case of derivative contracts)                                           |
| `SYMBOL_NAME`                   | `SM_SYMBOL_NAME`           | Symbol name of instrument                                                                                              |
| *removed*                       | `SEM_TRADING_SYMBOL`       | Exchange trading symbol of instrument                                                                                  |
| `DISPLAY_NAME`                  | `SEM_CUSTOM_SYMBOL`        | Dhan display symbol name of instrument                                                                                 |
| `INSTRUMENT_TYPE`               | `SEM_EXCH_INSTRUMENT_TYPE` | In addition to `INSTRUMENT` column, instrument type is defined by exchange adding more details about instrument        |
| `SERIES`                        | `SEM_SERIES`               | Exchange defined series for instrument                                                                                 |
| `LOT_SIZE`                      | `SEM_LOT_UNITS`            | Lot Size in multiples of which instrument is traded                                                                    |
| `SM_EXPIRY_DATE`                | `SEM_EXPIRY_DATE`          | Expiry date of instrument (applicable in case of derivative contracts)                                                 |
| `STRIKE_PRICE`                  | `SEM_STRIKE_PRICE`         | Strike Price of Options Contract                                                                                       |
| `OPTION_TYPE`                   | `SEM_OPTION_TYPE`          | Type of Options Contract   `CE` - Call   `PE` - Put                                                                    |
| `TICK_SIZE`                     | `SEM_TICK_SIZE`            | Minimum decimal point at which an instrument can be priced                                                             |
| `EXPIRY_FLAG`                   | `SEM_EXPIRY_FLAG`          | Type of Expiry (applicable in case of option contracts)   `M` - Monthly Expiry   `W` - Weekly Expiry                   |
| `BRACKET_FLAG`                  | -                          | Bracket order status   `N` - Not available   `Y` - Allowed                                                             |
| `COVER_FLAG`                    | -                          | Cover order status   `N` - Not available   `Y` - Allowed                                                               |
| `ASM_GSM_FLAG`                  | -                          | Flag for instrument is ASM or GSM   `N` - Not in ASM/GSM   `R` - Removed from block   `Y` - ASM/GSM                    |
| `ASM_GSM_CATEGORY`              | -                          | Category of instrument in ASM or GSM   `NA` in case of no surveillance                                                 |
| `BUY_SELL_INDICATOR`            | -                          | Indicator to show if Buy and Sell is allowed in instrument   `A` if both Buy/Sell is allowed                           |
| `BUY_CO_MIN_MARGIN_PER`         | -                          | Buy cover order minimum margin requirement (in percentage)                                                             |
| `SELL_CO_MIN_MARGIN_PER`        | -                          | Sell cover order minimum margin requirement (in percentage)                                                            |
| `BUY_CO_SL_RANGE_MAX_PERC`      | -                          | Buy cover order maximum range for stop loss leg (in percentage)                                                        |
| `SELL_CO_SL_RANGE_MAX_PERC`     | -                          | Sell cover order maximum range for stop loss leg (in percentage)                                                       |
| `BUY_CO_SL_RANGE_MIN_PERC`      | -                          | Buy cover order minimum range for stop loss leg (in percentage)                                                        |
| `SELL_CO_SL_RANGE_MIN_PERC`     | -                          | Sell cover order minimum range for stop loss leg (in percentage)                                                       |
| `BUY_BO_MIN_MARGIN_PER`         | -                          | Buy bracket order minimum margin requirement (in percentage)                                                           |
| `SELL_BO_MIN_MARGIN_PER`        | -                          | Sell bracket order minimum margin requirement (in percentage)                                                          |
| `BUY_BO_SL_RANGE_MAX_PERC`      | -                          | Buy bracket order maximum range for stop loss leg (in percentage)                                                      |
| `SELL_BO_SL_RANGE_MAX_PERC`     | -                          | Sell bracket order maximum range for stop loss leg (in percentage)                                                     |
| `BUY_BO_SL_RANGE_MIN_PERC`      | -                          | Buy bracket order minimum range for stop loss leg (in percentage)                                                      |
| `SELL_BO_SL_MIN_RANGE`          | -                          | Sell bracket order minimum range for stop loss leg (in percentage)                                                     |
| `BUY_BO_PROFIT_RANGE_MAX_PERC`  | -                          | Buy bracket order maximum range for target leg (in percentage)                                                         |
| `SELL_BO_PROFIT_RANGE_MAX_PERC` | -                          | Sell bracket order maximum range for target leg (in percentage)                                                        |
| `BUY_BO_PROFIT_RANGE_MIN_PERC`  | -                          | Buy bracket order minimum range for target leg (in percentage)                                                         |
| `SELL_BO_PROFIT_RANGE_MIN_PERC` | -                          | Sell bracket order minimum range for target leg (in percentage)                                                        |
| `MTF_LEVERAGE`                  | -                          | MTF Leverage available (in x multiple) for eligible `EQUITY` instruments                                               |

---
Copyright © 2024 Moneylicious Securities Private Limited

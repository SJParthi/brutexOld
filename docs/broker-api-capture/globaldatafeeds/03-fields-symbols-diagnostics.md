# GlobalDataFeeds APIs — API Fields, Symbol Naming Conventions & Diagnostic Responses

Master reference for fields returned by the APIs, symbol/instrument identifier naming formats per exchange, and diagnostic (error/informational) responses returned by the server.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/api-fields-description/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-conventions/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/

---

## API Fields Description

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/api-fields-description/ — last updated 2024-12-03T09:48:13 — captured 2026-07-15

| Field returned by API | Value (sample) | Description of the field |
| --- | --- | --- |
| AVERAGETRADEPRICE | 8905.09 | Average Traded Price(VWAP) |
| BUYPRICE | 8828.5 | Bid Price |
| BUYQTY | 150 | Bid Size |
| CLOSE | 9137.1 | Previous (Trading Day's) Close |
| EXCHANGE | "NFO" | Exchange |
| HIGH | 9138.7 | High |
| INSTRUMENTIDENTIFIER | "NIFTY-I" | Instrument Identifier (Symbol) |
| LASTTRADEPRICE | 8827.45 | Last Traded Price (Close) |
| LASTTRADEQTY | 2100 | Last Traded Quantity (Volume) |
| LASTTRADETIME | 1589796000000 | Last Trade Time since Epoch (Google "Epoch time" for more information or see [https://www.epochconverter.com/](https://www.epochconverter.com/) ) |
| LOW | 8811.05 | Low |
| OPEN | 9108 | Open |
| OPENINTEREST | 9022275 | Open Interest |
| PREOPEN | false | If Preopen is active |
| QUOTATIONLOT | 75 | Lot Size |
| SELLPRICE | 8830 | Ask Price |
| SELLQTY | 75 | Ask Size |
| SERVERTIME | 1589796000000 | Server Time since Epoch (Google "Epoch time" for more information or see [https://www.epochconverter.com/](https://www.epochconverter.com/) ) |
| TOTALQTYTRADED | 21517200 | Total Quantity Traded (Total Volume of the day) |
| VALUE | 191612602548 | Total Turnover |

---

## Symbol Naming Conventions

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-conventions/ — last updated 2025-04-03T11:26:48 — captured 2026-07-15

| Exchange | API Sample | Comments |
| --- | --- | --- |
| **NSE CM** | | |
| NSE Stocks | RELIANCE, AXISBANK | |
| NSE Indices | NIFTY 50, NIFTY BANK | |
| **NFO Futures Symbol Format (you may use any of the below 3 formats)** | | |
| **1.** NFO Futures (Continuous) | NIFTY-I, NIFTY-II, NIFTY-III RELIANCE-I, SBIN-I | Continuous Futures are available for NFO ("I" as in India). |
| **2.** NFO Futures (Contractwise /Short Format) | NIFTY27JAN22FUT BANKNIFTY27JAN22FUT RELIANCE27JAN22FUT | Contractwise Futures are also available for NFO. `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year last 2 digit>FUT` |
| **3.** NFO Futures (Contractwise /Long Format) | FUTIDX_NIFTY_27JAN2022_XX_0 FUTIDX_BANKNIFTY_27JAN2022_XX_0 FUTSTK_RELIANCE_27JAN2022_XX_0 | Contractwise Futures are also available for NFO. `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit><OptionType : CE for CALL, PE for PUT or XX if not applicable_<StrikePrice or 0 if not applicable>` |
| **NFO Options (you may use any of the below 2 formats)** | | |
| **1.** NFO Options (Contractwise **/** Short format) | NIFTY06JAN2217200CE RELIANCE27JAN222300PE SBIN27JAN22455CE AXISBANK27JAN22670CE BANKNIFTY27JAN2235000PE BANKNIFTY27JAN2235100CE | `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year in 2 digit><strike price><OptionType : CE for CALL, PE for PUT>` |
| **2.** NFO Options (Contractwise **/** Long format) | OPTIDX_NIFTY_27JAN2022_CE_17200 OPTSTK_RELIANCE_27JAN2022_PE_2300 OPTSTK_SBIN_27JAN2022_CE_455 OPTSTK_AXISBANK_27JAN2022_CE_670 OPTIDX_BANKNIFTY_27JAN2022_PE_35000 OPTIDX_BANKNIFTY_27JAN2022_CE_35100 | `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>_<OptionType: CE for CALL, PE for PUT or XX if not applicable>_<StrikePrice or 0 if not applicable>` |
| **MCX Futures (you may use any of the below 3 formats)** | | |
| **1.** MCX Futures (Continuous) | CRUDEOIL-I, CRUDEOIL-II, CRUDEOIL-III, COPPER-I GOLD-I | Continuous Futures for MCX ("I" as in India) |
| **2.** MCX Futures (Contractwise **/** Short format) | CRUDEOIL19SEP24FUT COPPER30SEP24FUT GOLD04OCT24FUT | Contractwise Futures are also available for NFO. `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year last 2 digit>FUT` |
| **3.** MCX Futures (Contractwise **/** Long format) | FUTCOM_CRUDEOIL_19JAN2022__0 FUTCOM_COPPER_31JAN2022__0 FUTCOM_GOLD_04FEB2022__0 | Contractwise Futures are also available for MCX. `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>` |
| **MCX Options (you may use any of the below 2 formats)** | | |
| **1.** MCX Options(Short Format) | CRUDEOIL17SEP2446500CE COPPER19SEP24820CE SILVER26NOV2479000PE | `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year in 2 digit><strike price><OptionType : CE for CALL, PE for PUT>` |
| **2.** MCX Options(Long Format) | OPTFUT_CRUDEOIL_17JAN2022_CE_5400 OPTFUT_COPPER_19JAN2022_CE_750 OPTFUT_SILVER_23FEB_2022_CE_66000 | `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>_<OptionType: CE for CALL, PE for PUT or XX if not applicable>_<StrikePrice or 0 if not applicable>` |
| **CDS Futures (you may use any of the below 3 formats)** | | |
| **1.** CDS Futures (Continuous) | USDINR-I, USDINR-II, USDINR-III GBPINR-I, JPYINR-I, EURINR-I | Continuous Futures for CDS ("I" as in India) |
| **2.** CDS Futures (Contractwise **/** Short Format) | USDINR28JAN22FUT USDINR25FEB22FUT EURINR28JAN22FUT GBPINR28JAN22FUT JPYINR28JAN22FUT | `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year in 2 digit>FUT` |
| **3.** CDS Futures (Contractwise **/** Long Format) | FUTCUR_USDINR_28JAN2022_XX_0 FUTCUR_USDINR_25FEB2022_XX_0 FUTCUR_EURINR_28JAN2022_XX_0 FUTCUR_GBPINR_28JAN2022_XX_0 FUTCUR_JPYINR_28JAN2022_XX_0 | `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>_<OptionType: CE for CALL, PE for PUT or XX if not applicable><StrikePrice or 0 if not applicable>` |
| **CDS Options (you may use any of the below 2 formats)** | | |
| CDS Weekly Options(Short Format) | USDINR21JAN2273.25PE USDINR28JAN2276.5CE EURINR27JAN2286CE GBPINR27JAN22100.5CE USDINR28JAN2276.5CE | `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year in 2 digit><strike price><CE for CALL Options and PE for PUT Options>` |
| CDS Weekly Options(Long Format) | OPTCUR_USDINR_21JAN2022_PE_73.25 OPTCUR_USDINR_28JAN2022_CE_76.5 OPTCUR_EURINR_27JAN2022_CE_86 OPTCUR_GBPINR_27JAN2022_CE_100.5 OPTCUR_USDINR_28JAN2022_CE_73.25 | `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>_<OptionType: CE for CALL, PE for PUT or XX if not applicable>_<StrikePrice or 0 if not applicable>` |
| **BSE CM** | | |
| BSE Stocks | ABB,RELIANCE | |
| BSE Indices | BANKEX,SENSEX | |
| **BFO Futures Symbol Format (you may use any of the below 3 formats)** | | |
| 1. BFO Futures (Continuous) | BANKEX-I, BANKEX-II, BANKEX-III AARTIIND-I | Continuous Futures are available for BFO ("I" as in India). |
| 2. BFO Futures (Contractwise /Short Format) | SENSEX28JUN24FUT BANKEX24JUN24FUT | Contractwise Futures are also available for BFO. `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year last 2 digit>FUT` |
| 3. BFO Futures (Contractwise /Long Format) | IF_BANKEX_28JUN2024_FF_0 IF_SENSEX_21JUN2024_FF_0 SF_AARTIIND_27JUN2024_FF_0 | Contractwise Futures are also available for BFO. `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit><OptionType : CE for CALL, PE for PUT or XX if not applicable_<StrikePrice or 0 if not applicable>`……………………………… |
| **BFO Options (you may use any of the below 2 formats)** | | |
| 1.BFO Options | SENSEX14JUN2476800CE SENSEX14JUN2475000PE BANKEX14JUN2456400CE BANKEX14JUN2457300PE | `<underlying><expiry date in 2 digit><expiry month in 3 characters><expiry year in 2 digit><strike price><OptionType : CE for CALL, PE for PUT>` |
| 2.BFO Options | IO_SENSEX_28JUN2024_PE_71800 IO_SENSEX_28JUN2024_CE_71800 IO_BANKEX_14JUN2024_PE_57300 IO_BANKEX_14JUN2024_CE_56400 | `<InstrumentType>_<underlying>_<expiry date in 2 digit>_<expiry month in 3 characters>_<expiry year in 4 digit>_<OptionType: CE for CALL, PE for PUT or XX if not applicable>_<StrikePrice or 0 if not applicable>` |

*(Note: multiple sample symbols within a single table cell are separated by spaces here; on the original page they were separated by line breaks.)*

---

## Diagnostic API Responses

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/ — last updated 2025-07-14T11:37:02 — captured 2026-07-15

- At times, server sends diagnostic responses instead of requested data. Please find below list of possible informational messages (returns as text/plain) by our server. Most of these responses are self-explanatory. **Your application should be designed to handle these responses which are returned as text/plain (instead of JSON / XML).**

| API Response | Description |
| --- | --- |
| Data unavailable. | There was some issue at server. Please try again or contact our team (with screenshot of error, actual request sent & paste actual key used) on developer@globaldatafeeds.in |
| Welcome! | Result of successful authentication |
| Authentication request received. Try request data in next moment. | This response is typically sent by REST API if requested data is not ready. So this passes the control back to your application. This is done because REST is stateless protocol without any callbacks. If this is not done and if server decides to wait, your application will feel like hang with poor user experience. When this response is received, you are supposed to send same request again in loop – till you receive requested data. |
| Access Denied. Key not found. | Please check the key used, key is not found at server |
| Access Denied. Key blocked. | Used key is blocked. Please contact our team (with screenshot of error, actual request sent & paste actual key used) on developer@globaldatafeeds.in |
| Access Denied. Key unknown or empty. | Please check the key used, key is not found at server |
| Key Expired. | Please check the key used, key is expired |
| Duplicated Request Not Allowed. | The same authentication request is sent more than once on the same session, which the server doesn't allow. |
| IP address not allowed. | The key used is configured to be used with specific Ips and your request is received from different IP. |
| Data for requested exchange is disabled. | The key is not authorised to request data from the Exchange used. Please make sure you are using valid Exchange value. |
| Reached instrument limitation. | The key used is requesting more symbols than authorised. To use new symbols, please reduce no. of used symbols. |
| Function not enabled. | The key is using API function which is not enabled for your account. If you feel this is error, please contact our team (with screenshot of error, actual request sent & paste actual key used) on developer@globaldatafeeds.in |
| RealtimeSubscription disabled for delayed data. | The key used is configured to be used for delayed data but instead, it is trying to use Realtime Data subscription |
| Bandwidth per hour is limited. | The key is using more bandwidth than configured value. The counter is reset every hour (e.g. 10AM, 11AM, 12PM, etc.) so if you receive this message, please try again after clock passes current hour. |
| Calls per hour are limited. | The key is using more no. of requests than configured value per hour. The counter is reset every hour (e.g. 10AM, 11AM, 12PM, etc.) so if you receive this message, please try again after clock passes current hour. |
| Calls limitation is reached. | The key is using more no. of requests than configured value for the month. |
| Bandwidth limitation is reached. | The key is using more bandwidth than configured value for the month. |
| Access Denied. Key Invalid. | Please check the key used, key is not found at server |
| Access Denied. Key already in use by other session. | Key is used in more than 1 session. Please ensure that key creates only 1 session. If any new sessions are created, previous sessions are invalidated with this message. |
| Selected periodicity or period disabled. | Please check value of "Periodicity" or "Period" used in your request. Either value specified is wrong or the key is not authorised to send requested Periodicity / Period |
| More than 1 match found. Use exact long format identifier instead. | Please use Long Identifier Symbol to request data. |
| Data unavailable. Send request after market close to get same day's data. | You are taken a subscription for after market data. To get the data for current day you need to send the request after current market is closed. |

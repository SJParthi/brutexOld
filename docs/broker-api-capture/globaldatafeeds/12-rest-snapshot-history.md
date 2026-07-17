# GlobalDataFeeds REST API — Snapshot & Historical Data Functions

Snapshot and historical data functions of the GlobalDataFeeds REST API: GetSnapshot, GetHistory, GetHistoryAfterMarket, GetExchangeSnapshot.

Sources:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getsnapshot/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/gethistoryaftermarket-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/

---

## GetSnapshot

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getsnapshot/ — last updated 2024-07-02T18:08:01 — captured 2026-07-15

### GetSnapshot : Returns latest Snapshot Data of multiple Symbols – max 25 in single call

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **periodicity** | *[MINUTE]/[HOUR], default = [MINUTE]* | String value of required periodicity. |
| **period** | *Numerical value 1, 2, 5…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR] periodicity types |
| **instrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetSnapshot/?accessKey=0a0b0c&exchange=MCX&periodicity=MINUTE&period=1&instrumentIdentifiers=FUTIDX_BANKNIFTY_25MAR2021_XX_0+FUTSTK_ACC_25MAR2021_XX_0&xml=true&isShortIdentifiers=true
```

(link target in original page: `http://endpoint:port/GetSnapshot/?accessKey=0a0b0c&exchange=MCX&periodicity=MINUTE&instrumentIdentifiers=FUTIDX_BANKNIFTY_25MAR2021_XX_0+FUTSTK_ACC_25MAR2021_XX_0&xml=true&isShortIdentifiers=false`)

**What is returned ?**

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
[{ "CLOSE": 17763.25, "EXCHANGE": "NFO", "HIGH": 17763.25, "INSTRUMENTIDENTIFIER": "FUTIDX_BANKNIFTY_25MAR2021_XX_0", "TRADEDQTY": 200, "LASTTRADETIME": 1434710918000, "LOW": 17763.25, "OPEN": 17763.25, "OPENINTEREST": 2633575},{ "CLOSE": 1419.95, "EXCHANGE": "NFO", "HIGH": 1419.95, "INSTRUMENTIDENTIFIER": "FUTSTK_ACC_25MAR2021_XX_0", "TRADEDQTY": 0, "LASTTRADETIME": 1434710916000, "LOW": 1419.95, "OPEN": 1419.95, "OPENINTEREST": 0}]
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<SnapshotArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="NFO" InstrumentIdentifier="FUTIDX_BANKNIFTY_25MAR2021_XX_0" LastTradeTime="6-19-2015 1:48:38 PM" TradedQty="200" OpenInterest="2633575" High="17763.25" Low="17763.25" Open="17763.25" Close="17763.25" />
<Value Exchange="NFO" InstrumentIdentifier="FUTSTK_ACC_25MAR2021_XX_0" LastTradeTime="6-19-2015 1:48:36 PM" TradedQty="0" OpenInterest="0" High="1419.95" Low="1419.95" Open="1419.95" Close="1419.95" />
</SnapshotArray>
```

CSV:

```csv
Close,Exchange,High,InstrumentIdentifier,TradedQty,LastTradeTime,Low,Open,OpenInterest
22385,NFO,22385,FUTIDX_NIFTY_25APR2024_XX_0,8550,1713851880000,22381,22381,9980700
```

---

## GetHistory

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/ — last updated 2025-10-20T09:54:11 — captured 2026-07-15

### GetHistory : Returns historical data (Tick / Minute / EOD)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **periodicity** | *[TICK]/[MINUTE]/[HOUR]/[DAY]/[WEEK]/[MONTH], default = [TICK]* | String value of required periodicity. |
| **period** | *Numerical value 1, 2, 3…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR]/[DAY] periodicity types |
| **from** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **to** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. |
| **userTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **AdjustSplits** | *[true]/[false], default = [true]* | Optional parameter. By default function will give you the data with Adjusted split, if we need the non-Adjusted data then we need to pass [AdjustSplits=false] then data will return with non-adjusted values . |

**Example**

```http
https://endpoint:port/GetHistory/? accessKey=your_api_key&exchang e=NFO&instrumentIdentifier=NIFT Y27JUN24FUT&periodicity=DAY&p eriod=1&max=10&from=17162697 00&to=1718861700&userTag=NIFT Y-I%20DAY&IsShortIdentifier=true&AdjustSplits=false
```

*(The example URL is reproduced character-for-character as published on the page, including the spaces introduced by line-wrapping in the original; without those wrap spaces it reads:)*

```http
https://endpoint:port/GetHistory/?accessKey=your_api_key&exchange=NFO&instrumentIdentifier=NIFTY27JUN24FUT&periodicity=DAY&period=1&max=10&from=1716269700&to=1718861700&userTag=NIFTY-I%20DAY&IsShortIdentifier=true&AdjustSplits=false
```

**What is returned ?**

**LastTradeTime, QuotationLot** (LotSize), **TradedQty, OpenInterest, Open, High, Low, Close**

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example For Supported Exchanges and their returns on (JSON,XML&CSV)**

| Format | View | Download |
|---|---|---|
| **JSON Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_JSON_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_JSON_FORMAT.zip) |
| **XML Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_XML_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_XML_FORMAT.zip) |
| **CSV Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_CSV_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_CSV_FORMAT.zip) |

---

## GetHistoryAfterMarket

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/gethistoryaftermarket-2/ — last updated 2024-07-03T08:22:47 — captured 2026-07-15

### GetHistoryAfterMarket : Returns historical data (Tick / Minute / EOD) till previous working day.

**What is GetHistoryAfterMarket?**

This function returns historical data (Tick / Minute / EOD) till **previous working day**. This function is useful for the users / service providers who want to provide services like backtesting – as they do not need live / current day's data. This should also save their API costs. To receive current day's historical data via this function, you will need to send request after market is closed. You can find details of the function request [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/)

---

## GetExchangeSnapshot

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/ — last updated 2024-07-03T08:24:29 — captured 2026-07-15

### GetExchangeSnapshot : Returns entire Exchange Snapshot as per Period & Periodicity

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **periodicity** | *Supported Values : Minute, Hour, Day* | Mandatory Parameter. |
| **period** | *Supported Values : 1,2,5,10,15,30* | Mandatory Parameter. |
| **instrumentType** | *like FUTIDX, FUTSTK, OPTIDX, OPTSTK, FUTCOM, FUTCUR, etc.* | Optional parameter. If absent, result is sent for all instrument types. How to get list of supported Instrument Types, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **From** | *like 1567655100* | Optional parameter. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot Start time. |
| **To** | *like 1567655100* | Optional parameter. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot End time.- This parameter is mandatory if "From" parameter is mentioned – Maximum 5 snapshots can be requested with single request |
| **nonTraded** | *[true]/[false], default [false]* | Optional parameter. When true, results are sent with data of even non traded instruments. When false, data of only traded instruments during that period is sent. Optional, default value is "false". Please note that this parameter should be sent with value "true" for NSE_IDX Exchange (NSE Indices) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetExchangeSnapshot/?accessKey=0a0b0c&exchange=NFO&periodicity=Minute&period=1
```

**What is returned ?**

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

**Example of returned data**

| Format | Downloads |
|---|---|
| **JSON** | [Download 1minute ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_1Min_JSON.zip) — [Download End-Of-Day ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_Day_JSON.zip) |
| **XML** | [Download 1minute ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_1Min_XML.zip) — [Download End-Of-Day ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_Day_XML.zip) |
| **CSV** | [Download 1minute ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_1min_CSV.zip) — [Download End-Of-Day ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_Day_CSV.zip) |

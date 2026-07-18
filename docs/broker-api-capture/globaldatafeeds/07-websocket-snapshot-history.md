# GlobalDataFeeds WebSockets API — Snapshot & Historical Data

Snapshot and historical data functions of the GlobalDataFeeds WebSockets API: GetSnapshot, GetHistory, GetHistoryAfterMarket and GetExchangeSnapshot.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/gethistoryaftermarket-3/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexchangesnapshot-2/

---

## GetSnapshot

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/ — last updated 2024-09-11T16:20:17 — captured 2026-07-15

### GetSnapshot : Returns latest Snapshot Data of multiple Symbols – max 25 in single call

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Periodicity** | *["MINUTE"]/["HOUR"], default = ["MINUTE"]* | String value of required periodicity. |
| **Period** | *Numerical value 1, 2, 3…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR] periodicity types |
| **InstrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetSnapshot",
                Exchange: "MCX",
                Periodicity: "MINUTE",
                Period: 1,
				isShortIdentifiers: "true",
                InstrumentIdentifiers: [{Value:"CRUDEOIL19MAR21FUT"}, {Value:"NATURALGAS26MAR21FUT"}]
            };
     doSend(request);
```

### Example of returned data in JSON format

```json
{"Result":[
{"InstrumentIdentifier":"CRUDEOIL19MAR21FUT",
"Exchange":"MCX",
"LastTradeTime":1593008580,
"TradedQty":167,
"OpenInterest":3760,
"Open":3001.0,
"High":3003.0,
"Low":2998.0,
"Close":3000.0},
{"InstrumentIdentifier":"NATURALGAS26MAR21FUT",
"Exchange":"MCX",
"LastTradeTime":1593008580,
"TradedQty":57,
"OpenInterest":10824,
"Open":129.8,
"High":129.9,
"Low":129.7,
"Close":129.8}],
"MessageType":"SnapshotResult"}
```

### Important Note:

GetSnapshot , the time when request is sent is very important. For example : If user sends request for 1minute snapshot data at 9:20:01, he will get record of trade data between 9:19:00 to 9:20:00. However, if request is sent at 9:19:58 i.e. before minute is complete, user will get trade data between 9:18:00 to 9:19:00. In both cases, timestamp of the returned data will be start timestamp of the period. So when data between 9:15:00 to 9:20:00 is returned, it will have timestamp of 9:15:00. Also if a user subscribes for snapshot data of any symbol for a period and if no trades happen during that period, no data is sent by the server

---

## GetHistory

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/ — last updated 2025-10-20T09:56:00 — captured 2026-07-15

### GetHistory : Returns historical data (Tick / Minute / EOD)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *["TICK"]/["MINUTE"]/["HOUR"]/["DAY"]/["WEEK"]/["MONTH"], default = ["TICK"]* | String value of required periodicity. |
| **Period** | *Numerical value 1, 2, 3…, default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR]/[DAY] periodicity types |
| **From** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **To** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **Max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. |
| **UserTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| I**sShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **AdjustSplits** | *[true]/[false], default = [true]* | Optional parameter. By default function will give you the data with Adjusted, if we need the non-Adjusted data then we need to pass [AdjustSplits=false] then data will return with non-adjusted values . |

### What is returned ?

**LastTradeTime, QuotationLot** (LotSize), **TradedQty, OpenInterest, Open, High, Low, Close**

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request and response

| | | |
|---|---|---|
| JavaScript | [View](https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Javascript_Sample_AllFunctions.pdf) | Download PDF file - [Click Here](https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Javascript_Sample_AllFunctions.zip) |
| Python | [View](https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.pdf) | Download PDF file - [Click Here](https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.zip) |
| Java Source | | |

---

## GetHistoryAfterMarket

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/gethistoryaftermarket-3/ — last updated 2024-07-12T11:03:38 — captured 2026-07-15

### GetHistoryAfterMarket : Returns historical data (Tick / Minute / EOD) till previous working day.

### What is GetHistoryAfterMarket?

This function returns historical data (Tick / Minute / EOD) till **previous working day**. This function is useful for the users / service providers who want to provide services like backtesting – as they do not need live / current day's data. This should also save their API costs. To receive current day's historical data via this function, you will need to send request after market is closed. You can find details of the function request [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/)

---

## GetExchangeSnapshot

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexchangesnapshot-2/ — last updated 2024-09-16T14:04:53 — captured 2026-07-15

### GetExchangeSnapshot : Returns entire Exchange Snapshot as per Period & Periodicity

### Supported parameters

| | | |
|---|---|---|
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **periodicity** | *Supported Values : Minute, Hour, Day* | Mandatory Parameter. |
| **period** | *Supported Values : 1,2,5,10,15,30* | Mandatory Parameter. |
| **instrumentType** | *like FUTIDX, FUTSTK, OPTIDX, OPTSTK, FUTCOM, FUTCUR, etc.* | Optional parameter. If absent, result is sent for all instrument types. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **From** | *like 1567655100* | Optional parameter. Epoch value of time in seconds since 1st January 1970. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot Start time. |
| **To** | *like 1567655100* | Optional parameter. Epoch value of time in seconds since 1st January 1970. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot End time.- This parameter is mandatory if "From" parameter is mentioned<br>– Maximum 5 snapshots can be requested with single request |
| **nonTraded** | *[true]/[false], default [false]* | Optional parameter. When true, results are sent with data of even non traded instruments. When false, data of only traded instruments during that period is sent. Optional, default value is "false"Please note that this parameter should be sent with value "true" for NSE_IDX Exchange (NSE Indices) |

### What is returned ?

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetExchangeSnapshot",
Exchange: "NFO",
Periodicity: "Minute",
Period: 1
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

[Download 1minute ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_1Min_JSON.zip)
[Download End-Of-Day ExchangeSnapshot](https://globaldatafeeds.in/resources/GetExchangeSnapshot_Day_JSON.zip)

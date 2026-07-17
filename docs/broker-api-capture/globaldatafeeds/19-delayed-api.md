# GlobalDataFeeds Delayed API

Delayed Data API — delayed snapshot, exchange snapshot and historical data functions (WebSocket and REST). Delay parameter is configured at the GFDL backend, so request syntax and response fields match the realtime functions.

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/subscribesnapshot-delayed/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getsnapshot-delayed/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/gethistory-delayed/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getexchangesnapshot-delayed/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/gethistory-delayed-returns-historical-data-tick-minute-eod/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/getsnapshot-delayed-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/getexchangesnapshot-delayed-2/

---

## SubscribeSnapshot (Delayed) (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/subscribesnapshot-delayed/ — last updated 2024-11-22T14:53:06 — captured 2026-07-15

### SubscribeSnapshot : Returns snapshot data as per Periodicity & Period values with delay.

Delay parameter is configured at our backend. Hence this function works exactly in same manner like [SubscribeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscribesnapshot/). So the request syntax and fields in response remain exactly same like [SubscribeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscribesnapshot/). Only difference is that the data is delayed which will be visible only from the trade timestamp received in the response (field : LastTradeTime) and comparing it with the current time.

### Supported parameters

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *["MINUTE"]/["HOUR"]* | String value of required periodicity. |
| **Period** | *1,2,5,10,15,30 default = 1* | Numerical value of required Period. |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. Buy default subscribes to Realtime data. If [true] instrumentIdentifier is unsubscribed |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier** (Symbol), **Periodicity, Period, LastTradeTime, Open, High, Low, Close, TradedQty, OpenInterest** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
{
MessageType: "SubscribeSnapshot",
Exchange: "NFO",
InstrumentIdentifier: "NIFTY-I",
Periodicity: "MINUTE",
Period: 1
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format. This data returned as per Periodicity & Period values**

```text
Note: 
For example: If the API key is configured with a 15-minute delay for NFO.
Request sent at September 13, 2024  11:40:00 (IST)
Response received at
11:41:01 - Last Trade Time (IST): September 13, 2024 11:26:00 AM
11:42:01 - Last Trade Time (IST): September 13, 2024 11:27:00 AM
```

```text
RESPONSE: {"Exchange":"NFO", 
"InstrumentIdentifier":"NIFTY-I",
"Periodicity":"MINUTE",
"Period":1,
"LastTradeTime":1726206960,
"TradedQty":10450,
"OpenInterest":14626675,
"Open":25391.75,
"High":25392.5,
"Low":25386.0,
"Close":25386.2,
"MessageType":"RealtimeSnapshotResult"}

RESPONSE: {"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY-I",
"Periodicity":"MINUTE",
"Period":1,
"LastTradeTime":1726207020,
"TradedQty":13475,
"OpenInterest":14636550,
"Open":25386.25,
"High":25388.4,
"Low":25378.5,
"Close":25380.0,
"MessageType":"RealtimeSnapshotResult"}
```

---

## GetSnapshot (Delayed) (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getsnapshot-delayed/ — last updated 2024-11-22T14:58:30 — captured 2026-07-15

### GetSnapshot (Delayed): Returns latest Snapshot Data of multiple Symbols – max 25 in single call with Delay (only active during market hours)

Delay parameter is configured at our backend. Hence this function works exactly in same manner like [GetSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/). So the request syntax and fields in response remain exactly same like [GetSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/). Only difference is that the data is delayed which will be visible only from the trade timestamp received in the response (field : LastTradeTime) and comparing it with the current time.

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Periodicity** | *["MINUTE"]/["HOUR"], default = ["MINUTE"]* | String value of required periodicity. |
| **Period** | *Numerical value 1, 2, 3…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR] periodicity types |
| **InstrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**What is returned ?**

| |
| --- |
| **InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
            {
                MessageType: "GetSnapshot",
                Exchange: "MCX",
                Periodicity: "MINUTE",
                Period: 1,
		isShortIdentifiers: "true",
                InstrumentIdentifiers: [{Value:"FUTCOM_NATURALGAS_25SEP2024__0"}, {Value:"FUTCOM_CRUDEOIL_19SEP2024__0"}]
            };
     doSend(request);
```

**Example of returned data in JSON format**

```text
Time : Mon Sep 16 2024 14:30:04 GMT+0530 (India Standard Time)
SENT: {"MessageType":"GetSnapshot","Exchange":"MCX","Periodicity":"MINUTE","Period":1,"InstrumentIdentifiers":[{"Value":"NATURALGAS-I"},{"Value":"CRUDEOIL-I"}]}

Time : Mon Sep 16 2024 14:30:04 GMT+0530 (India Standard Time)
RESPONSE: 
{"Result":[{"InstrumentIdentifier":"FUTCOM_CRUDEOIL_19SEP2024__0",
"Exchange":"MCX",
"LastTradeTime":1726476900,
"TradedQty":4,
"OpenInterest":9770,
"Open":5826.0,
"High":5828.0,
"Low":5826.0,
"Close":5828.0,
"TokenNumber":null},{"InstrumentIdentifier":"FUTCOM_NATURALGAS_25SEP2024__0",
"Exchange":"MCX",
"LastTradeTime":1726476900,
"TradedQty":29,
"OpenInterest":34445,
"Open":192.7,
"High":192.7,
"Low":192.7,
"Close":192.7
,"TokenNumber":null}],
"MessageType":"SnapshotResult"}
```

**Important Note:**

GetSnapshot (Delayed), the time when request is sent is very important. 

For example: If the API key is configured with a 5-minute delay for MCX.
Request sent at September 16, 2024 14:30:04 (IST) 
Response received at
14:30:04 – Last Trade Time (IST): September 16, 2024 14:25:00 PM For example : If user sends request for 1minute snapshot data at 14:30:01, user will get record of trade data of 14:25:00. However, if request is sent at 14:30:04 i.e. before minute is complete, user will get trade data between 14:25:00 to 14:26:00. In both cases, timestamp of the returned data will be start timestamp of the period. So when data between 14:25:00 to 14:26:00 is returned, it will have timestamp of 14:25:00.

---

## GetHistory (Delayed) (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/gethistory-delayed/ — last updated 2024-11-22T14:52:24 — captured 2026-07-15

### GetHistory : Returns delayed historical data (Tick / Minute / EOD)

Delay parameter is configured at our backend. Hence this function works exactly in same manner like [GetHistory](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/). So the request syntax and fields in response remain exactly same like [GetHistory](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/). Only difference is that the data is delayed which will be visible only from the trade timestamp received in the response (field : LastTradeTime) and comparing it with the current time.

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *["TICK"]/["MINUTE"]/["HOUR"]/["DAY"]/["WEEK"]/["MONTH"], default = ["TICK"]* | String value of required periodicity. |
| **Period** | *Numerical value 1, 2, 3…, default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR]/[DAY] periodicity types |
| **From** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **To** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **Max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. |
| **userTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**What is returned ?**

| |
| --- |
| **LastTradeTime, QuotationLot** (LotSize), **TradedQty, OpenInterest, Open, High, Low, Close** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
{
                MessageType: "GetHistory",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
                Periodicity: "MINUTE",
                Period: 5,
                Max: 10,
                isShortIdentifier: "False",
                UserTag : "demouser"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data (OHLC) in JSON format**

```text
Time : Wed Sep 11 2024 12:21:31 GMT+0530 (India Standard Time)
        RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","IsShortIdentifier":false,"From":0,"To":0,"Max":10,"Periodicity":"MINUTE","Period":5,"UserTag":null,"AdjustSplits":true,"MessageType":"GetHistory"},
        "Result":[{"LastTradeTime":1726036500,"QuotationLot":25,"TradedQty":64500,"OpenInterest":13405900,"Open":25120.65,"High":25138.8,"Low":25113.65,"Close":25115.45},
        {"LastTradeTime":1726036200,"QuotationLot":25,"TradedQty":85850,"OpenInterest":13400800,"Open":25108.0,"High":25127.95,"Low":25108.0,"Close":25120.65},
        {"LastTradeTime":1726035900,"QuotationLot":25,"TradedQty":54300,"OpenInterest":13401975,"Open":25088.6,"High":25115.0,"Low":25088.6,"Close":25108.0},
        {"LastTradeTime":1726035600,"QuotationLot":25,"TradedQty":40250,"OpenInterest":13397325,"Open":25093.05,"High":25104.0,"Low":25086.55,"Close":25088.45},
        {"LastTradeTime":1726035300,"QuotationLot":25,"TradedQty":31950,"OpenInterest":13389625,"Open":25090.55,"High":25098.0,"Low":25080.05,"Close":25094.85},
        {"LastTradeTime":1726035000,"QuotationLot":25,"TradedQty":29925,"OpenInterest":13379725,"Open":25081.1,"High":25095.0,"Low":25075.05,"Close":25090.55},
        {"LastTradeTime":1726034700,"QuotationLot":25,"TradedQty":41025,"OpenInterest":13376250,"Open":25090.45,"High":25094.0,"Low":25062.15,"Close":25082.95},
        {"LastTradeTime":1726034400,"QuotationLot":25,"TradedQty":26825,"OpenInterest":13372525,"Open":25086.9,"High":25096.0,"Low":25080.5,"Close":25090.75},
        {"LastTradeTime":1726034100,"QuotationLot":25,"TradedQty":17350,"OpenInterest":13368300,"Open":25078.15,"High":25091.0,"Low":25075.8,"Close":25086.25},
        {"LastTradeTime":1726033800,"QuotationLot":25,"TradedQty":60450,"OpenInterest":13382200,"Open":25079.55,"High":25082.85,"Low":25068.35,"Close":25078.25}],
        "MessageType":"HistoryOHLCResult"}
```

**Sample request by taking FROM & TO parameter(JavaScript)**

```javascript
{
               MessageType: "GetHistory",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
                Periodicity: "MINUTE",
		From: 1725942600,
		To: 1725943800,
                Period: 10,
		isShortIdentifier: "False",
                UserTag : "demouser"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data (OHLC) in JSON format**

```text
Time : Tue Sep 10 2024 21:24:39 GMT+0530 (India Standard Time)
    RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","IsShortIdentifier":false,"From":1725942600,"To":1725943800,"Max":0,"Periodicity":"MINUTE","Period":10,"UserTag":null,"AdjustSplits":true,"MessageType":"GetHistory"},
    "Result":[{"LastTradeTime":1725943800,"QuotationLot":25,"TradedQty":71275,"OpenInterest":13879850,"Open":24944.5,"High":24966.9,"Low":24940.6,"Close":24959.9},
    {"LastTradeTime":1725943200,"QuotationLot":25,"TradedQty":100250,"OpenInterest":13889050,"Open":24942.15,"High":24949.95,"Low":24919.5,"Close":24938.8},
    {"LastTradeTime":1725942600,"QuotationLot":25,"TradedQty":126200,"OpenInterest":13871875,"Open":24943.45,"High":24959.05,"Low":24930.0,"Close":24941.7}],
    "MessageType":"HistoryOHLCResult"}
```

**Sample request for hour timeframe using FROM & TO parameter(JavaScript)**

```javascript
{
                MessageType: "GetHistory",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
                Periodicity: "Hour",
		From: 1725942600,
		To: 1725949800,
		UserTag : "demo_user"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data (OHLC) in JSON format**

```text
Time : Wed Sep 11 2024 11:43:31 GMT+0530 (India Standard Time)
        RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","IsShortIdentifier":false,"From":1725942600,"To":1725949800,"Max":0,"Periodicity":"HOUR","Period":1,"UserTag":null,"AdjustSplits":true,"MessageType":"GetHistory"},
        "Result":[{"LastTradeTime":1725949800,"QuotationLot":25,"TradedQty":1029450,"OpenInterest":13730625,"Open":25062.95,"High":25124.0,"Low":25047.0,"Close":25102.75},
        {"LastTradeTime":1725946200,"QuotationLot":25,"TradedQty":675800,"OpenInterest":13886750,"Open":24991.6,"High":25067.9,"Low":24978.75,"Close":25062.95},
        {"LastTradeTime":1725942600,"QuotationLot":25,"TradedQty":555175,"OpenInterest":13930900,"Open":24943.45,"High":24996.0,"Low":24919.5,"Close":24995.3}],
        "MessageType":"HistoryOHLCResult"}
```

**Sample request in TICK format(JavaScript)**

```javascript
{
                MessageType: "GetHistory",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
                Periodicity: "TICK",
                Period: 1,  // Period is mandatory
		Max: 5,
		isShortIdentifier: "False",
                UserTag : "demouser"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data (Tick) in JSON format**

```text
Time : Wed Sep 11 2024 11:55:04 GMT+0530 (India Standard Time)
        RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","IsShortIdentifier":false,"From":0,"To":0,"Max":5,"Periodicity":"TICK","Period":1,"UserTag":null,"AdjustSplits":true,"MessageType":"GetHistory"},
        "Result":[{"LastTradeTime":1726035004,"LastTradePrice":25083.9,"QuotationLot":25,"TradedQty":50,"OpenInterest":13377500,"BuyPrice":25081.15,"BuyQty":50,"SellPrice":25083.9,"SellQty":25},
        {"LastTradeTime":1726035002,"LastTradePrice":25084.0,"QuotationLot":25,"TradedQty":450,"OpenInterest":13376250,"BuyPrice":25080.6,"BuyQty":50,"SellPrice":25084.3,"SellQty":100},
        {"LastTradeTime":1726035001,"LastTradePrice":25081.1,"QuotationLot":25,"TradedQty":150,"OpenInterest":13376250,"BuyPrice":25080.45,"BuyQty":50,"SellPrice":25082.15,"SellQty":50},
        {"LastTradeTime":1726034999,"LastTradePrice":25082.95,"QuotationLot":25,"TradedQty":50,"OpenInterest":13376250,"BuyPrice":25081.6,"BuyQty":125,"SellPrice":25083.0,"SellQty":50},
        {"LastTradeTime":1726034997,"LastTradePrice":25080.0,"QuotationLot":25,"TradedQty":0,"OpenInterest":13376250,"BuyPrice":25080.75,"BuyQty":500,"SellPrice":25082.95,"SellQty":75}],
        "MessageType":"HistoryTickResult"}
```

**Sample request in DAY format(JavaScript)**

```javascript
{
                MessageType: "GetHistory",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
                Periodicity: "DAY",
		Max: 5,
                UserTag : "demouser"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data (OHLC) in JSON format**

```text
Time : Wed Sep 11 2024 12:14:08 GMT+0530 (India Standard Time)
        RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","IsShortIdentifier":false,"From":0,"To":0,"Max":5,"Periodicity":"DAY","Period":1,"UserTag":null,"AdjustSplits":true,"MessageType":"GetHistory"},
        "Result":[{"LastTradeTime":1725971400,"QuotationLot":25,"TradedQty":6026375,"OpenInterest":13288750,"Open":25034.0,"High":25174.0,"Low":24919.5,"Close":25083.0},
        {"LastTradeTime":1725885000,"QuotationLot":25,"TradedQty":5348700,"OpenInterest":13714275,"Open":24865.35,"High":25000.8,"Low":24816.0,"Close":24985.0},
        {"LastTradeTime":1725625800,"QuotationLot":25,"TradedQty":10407125,"OpenInterest":14273075,"Open":25189.8,"High":25219.0,"Low":24855.0,"Close":24906.0},
        {"LastTradeTime":1725539400,"QuotationLot":25,"TradedQty":3922150,"OpenInterest":14884750,"Open":25350.0,"High":25351.0,"Low":25225.0,"Close":25236.75},
        {"LastTradeTime":1725453000,"QuotationLot":25,"TradedQty":5544900,"OpenInterest":15240000,"Open":25210.15,"High":25275.0,"Low":25142.35,"Close":25247.8}],
        "MessageType":"HistoryOHLCResult"}
```

**Important Note**

As per user requirement of data for the delayed datafeed, changes are done at GFDL's backend. So users will not need to make any changes in their code. The request syntax and response fields will remain the same for realtime as well as delayed history.

---

## GetExchangeSnapshot (Delayed) (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getexchangesnapshot-delayed/ — last updated 2025-02-13T12:42:30 — captured 2026-07-15

### GetExchangeSnapshot (Delayed):Returns entire Exchange Snapshot as per Period & Periodicity with delay

Delay parameter is configured at our backend. Hence this function works exactly in same manner like GetExchangeSnapshot. So the request syntax and fields in response remain exactly same like [GetExchangeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/). Only difference is that the data is delayed which will be visible only from the trade timestamp received in the response (field : LastTradeTime) and comparing it with the current time.

**Supported parameters**

| | | |
| --- | --- | --- |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **periodicity** | *Supported Values : Minute, Hour, Day* | Mandatory Parameter. |
| **period** | *Supported Values : 1,2,5,10,15,30* | Mandatory Parameter. |
| **instrumentType** | *like FUTIDX, FUTSTK, OPTIDX, OPTSTK, FUTCOM, FUTCUR, etc.* | Optional parameter. If absent, result is sent for all instrument types. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **From** | *like 1567655100* | Optional parameter. Epoch value of time in seconds since 1st January 1970. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot Start time. |
| **To** | *like 1567655100* | Optional parameter. Epoch value of time in seconds since 1st January 1970. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot End time. – This parameter is mandatory if "From" parameter is mentioned – Maximum 5 snapshots can be requested with single request |
| **nonTraded** | *[true]/[false], default [false]* | Optional parameter. When true, results are sent with data of even non traded instruments. When false, data of only traded instruments during that period is sent. Optional, default value is "false" Please note that this parameter should be sent with value "true" for NSE_IDX Exchange (NSE Indices) |

**What is returned ?**

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

**Sample request(JavaScript)**

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

**Example of returned data in JSON format**

[Download 1minute ExchangeSnapshotDelayed](https://globaldatafeeds.in/resources/GFDL-GetExchangeSnapshot_Delayed_Response.zip)
[Download End-Of-Day ExchangeSnapshotDelayed](https://staging-gfdlmain.kinsta.cloud/wp-content/uploads/2024/09/EODExchangeSnapshotDelayed.zip)

---

## GetHistory (Delayed) (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/gethistory-delayed-returns-historical-data-tick-minute-eod/ — last updated 2024-12-30T12:01:04 — captured 2026-07-15

GetHistory : Returns historical data (Tick / Minute / EOD)

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **periodicity** | *[TICK]/[MINUTE]/[HOUR]/[DAY]/[WEEK]/[MONTH], default = [TICK]* | String value of required periodicity. |
| **period** | *Numerical value 1, 2, 3…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR]/[DAY] periodicity types |
| **from** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **to** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. |
| **userTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **Example** | `https://endpoint:port/GetHistory/? accessKey=your_api_key&exchang e=NFO&instrumentIdentifier=NIFT Y27JUN24FUT&periodicity=DAY&p eriod=1&max=10&from=17162697 00&to=1718861700&userTag=NIFT Y-I%20DAY&IsShortIdentifier=true&AdjustSplits=false` | |

**What is returned ?**

| |
| --- |
| **LastTradeTime, QuotationLot** (LotSize), **TradedQty, OpenInterest, Open, High, Low, Close** |
| LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example For Supported Exchanges and their returns on (JSON,XML&CSV)**

| | | |
| --- | --- | --- |
| **JSON Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_JSON_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_JSON_FORMAT.zip) |
| **XML Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_XML_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_XML_FORMAT.zip) |
| **CSV Format** | [View](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_CSV_FORMAT.pdf) | Download PDF File – [Click Here](https://globaldatafeeds.in/resources/API_Example_pdf/GFDL_REST_GetHistory_CSV_FORMAT.zip) |

---

## GetSnapshot (Delayed) (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/getsnapshot-delayed-2/ — last updated 2025-01-27T11:06:19 — captured 2026-07-15

### GetSnapshot (Delayed): Returns latest Snapshot Data of multiple Symbols – max 25 in single call with Delay (only active during market hours)

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **periodicity** | *[MINUTE]/[HOUR], default = [MINUTE]* | String value of required periodicity. |
| **period** | *Numerical value 1, 2, 5…l default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[HOUR] periodicity types |
| **instrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here.](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **Example** | [http://endpoint:port/GetSnapshot/?accessKey=0a0b0c&exchange=NFO&periodicity=MINUTE&period=1&instrumentIdentifiers=NIFTY-I+BANKNIFTY-I&xml=true](http://endpoint:port/GetSnapshot/?accessKey=0a0b0c&exchange=NFO&periodicity=MINUTE&period=1&instrumentIdentifiers=NIFTY-I+BANKNIFTY-I&xml=true) | |

**What is returned ?**

| |
| --- |
| **InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close** |
| LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Important Note:**

GetSnapshot (Delayed), the time when request is sent is very important.

For example: If the API key is configured with a 5-minute delay for NFO.
Request sent at January 27, 2025 10:16:00 AM (IST)
Response received at
10:16:00 – Last Trade Time (IST): January 27, 2025 10:11:00 AM For example : If user sends request for 1minute snapshot data at  10:16:00, user will get record of trade data of 10:11:00. However, if request is sent at 10:15:46 i.e. before minute is complete, user will get trade data between 10:10:00 to 10:11:00. In both cases, timestamp of the returned data will be start timestamp of the period. So when data between 10:10:00 to  10:11:00 is returned, it will have timestamp of 10:10:00.

**Example of returned data**

**JSON**

```text
0
CLOSE 22967.75
EXCHANGE "NFO"
HIGH 22982.3
INSTRUMENTIDENTIFIER "FUTIDX_NIFTY_30JAN2025_XX_0"
TRADEDQTY 9650
LASTTRADETIME 1737952860000
LOW 22964
OPEN 22982.3
OPENINTEREST 13127325
1
CLOSE 48145.35
EXCHANGE "NFO"
HIGH 48183.55
INSTRUMENTIDENTIFIER "FUTIDX_BANKNIFTY_30JAN2025_XX_0"
TRADEDQTY 5880
LASTTRADETIME 1737952860000
LOW 48130.1
OPEN 48183.55
OPENINTEREST 2284410
```

**XML**

```xml
<SnapshotArray>
<Value Exchange="NFO" InstrumentIdentifier="FUTIDX_NIFTY_30JAN2025_XX_0″ LastTradeTime="01-27-2025 10:08:00″ TradedQty="11825″ OpenInterest="13121050″ High="22995″ Low="22971.2″ Open="22993.15″ Close="22976.55″/>
<Value Exchange="NFO" InstrumentIdentifier="FUTIDX_BANKNIFTY_30JAN2025_XX_0″ LastTradeTime="01-27-2025 10:08:00″ TradedQty="4320″ OpenInterest="2283510″ High="48224.5″ Low="48175.05″ Open="48224.5″ Close="48180.5″/>
</SnapshotArray>
10:39 am
```

**CSV**

```csv
Close,Exchange,High,InstrumentIdentifier,TradedQty,LastTradeTime,Low,Open,OpenInterest 22980,NFO,22980,FUTIDX_NIFTY_30JAN2025_XX_0,9575,1737952980000,22965,22971.9,13127875 48159.35,NFO,48159.35,FUTIDX_BANKNIFTY_30JAN2025_XX_0,3330,1737952980000,48121,48149.95,2285505
```

---

## GetExchangeSnapshot (Delayed) (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api-rest-api-documentation/getexchangesnapshot-delayed-2/ — last updated 2025-01-31T15:44:25 — captured 2026-07-15

### GetExchangeSnapshot (Delayed):Returns entire Exchange Snapshot as per Period & Periodicity with delay

Delay parameter is configured at our backend. Hence this function works exactly in same manner like [GetExchangeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/). So the request syntax and fields in response remain exactly same like [GetExchange](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/)[Snapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/). Only difference is that the data is delayed which will be visible only from the trade timestamp received in the response (field : LastTradeTime) and comparing it with the current time.

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **periodicity** | *Supported Values : Minute, Hour, Day* | Mandatory Parameter. |
| **period** | *Supported Values : 1,2,5,10,15,30* | Mandatory Parameter. |
| **instrumentType** | *like FUTIDX, FUTSTK, OPTIDX, OPTSTK, FUTCOM, FUTCUR, etc.* | Optional parameter. If absent, result is sent for all instrument types. How to get list of supported Instrument Types, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **From** | *like 1567655100* | Optional parameter. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot Start time. |
| **To** | *like 1567655100* | Optional parameter. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page). For example, 1567655100 is epoch value for Thursday, September 5, 2019 9:15:00 AM in GMT+05:30 timezone. This is Optional field to control snapshot End time.- This parameter is mandatory if "From" parameter is mentioned – Maximum 5 snapshots can be requested with single request |
| **nonTraded** | *[true]/[false], default [false]* | Optional parameter. When true, results are sent with data of even non traded instruments. When false, data of only traded instruments during that period is sent. Optional, default value is "false"Please note that this parameter should be sent with value "true" for NSE_IDX Exchange (NSE Indices) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **Example** | [http://endpoint:port/GetExchangeSnapshot/?accessKey=0a0b0c&exchange=NFO&periodicity=Minute&period=1](http://endpoint:port/GetExchangeSnapshot/?accessKey=0a0b0c&exchange=NFO&periodicity=Minute&period=1) | |

**What is returned ?**

**InstrumentIdentifier** (Symbol), **Exchange, LastTradeTime, TradedQty, OpenInterest, Open, High, Low, Close**

**Example of returned data**

| | |
| --- | --- |
| **JSON** | |
| **XML** | |
| **CSV** | |

# GlobalDataFeeds WebSockets API — Server Info & Messages

Server/account informational functions of the GlobalDataFeeds WebSockets API: GetServerInfo, GetLimitation, GetMarketMessages and GetExchangeMessages.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getserverinfo-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlimitation-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getmarketmessages-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchangemessages-2/

---

## GetServerInfo

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getserverinfo-2/ — last updated 2024-07-10T12:37:24 — captured 2026-07-15

### GetServerInfo : Returns information about server where connection is made

### What is returned ?

**Server EndPoint where enduser is connected (useful while debugging issues)**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetServerInfo",
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"ServerID":"2152-24",
"MessageType":"ServerInfoResult"
}
```

---

## GetLimitation

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlimitation-2/ — last updated 2024-07-10T12:37:17 — captured 2026-07-15

### GetLimitation : Returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.)

### What is returned ?

**Returns details about user account (what is allowed / disallowed)**

### Example send request(JavaScript)

```javascript
var request =
{
MessageType: "GetLimitation"
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data

```json
{
"GeneralParams":{
"AllowedBandwidthPerHour":-1.0,
"AllowedCallsPerHour":-1,
"AllowedCallsPerMonth":-1,
"AllowedBandwidthPerMonth":-1.0,
"ExpirationDate":1485468000,
"Enabled":true
},<
"AllowedExchanges":[
{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"CDS"},
{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"MCX"},
{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NFO"},
{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NSE"},
{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NSE_IDX"}
],
"AllowedFunctions":[
{"FunctionName":"GetExchangeMessages","IsEnabled":false},
{"FunctionName":"GetHistory","IsEnabled":true},
{"FunctionName":"GetLastQuote","IsEnabled":false},
{"FunctionName":"GetLastQuoteArray","IsEnabled":false},
{"FunctionName":"GetLastQuoteArrayShort","IsEnabled":false},
{"FunctionName":"GetLastQuoteShort","IsEnabled":false},
{"FunctionName":"GetMarketMessages","IsEnabled":false},
{"FunctionName":"GetSnapshot","IsEnabled":true},
{"FunctionName":"SubscribeRealtime","IsEnabled":false}
],
"HistoryLimitation":{
"TickEnabled":true,
"DayEnabled":true,
"WeekEnabled":false,
"MonthEnabled":false,
"MaxEOD":100000,
"MaxIntraday":44,
"Hour_1Enabled":false,
"Hour_2Enabled":false,
"Hour_3Enabled":false,
"Hour_4Enabled":false,
"Hour_6Enabled":false,
"Hour_8Enabled":false,
"Hour_12Enabled":false,
"Minute_1Enabled":true,
"Minute_2Enabled":false,
"Minute_3Enabled":false,
"Minute_4Enabled":false,
"Minute_5Enabled":false,
"Minute_6Enabled":false,
"Minute_10Enabled":false,
"Minute_12Enabled":false,
"Minute_15Enabled":false,
"Minute_20Enabled":false,
"Minute_30Enabled":false,
"MaxTicks":5
},
"MessageType":"LimitationResult",
}
```

---

## GetMarketMessages

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getmarketmessages-2/ — last updated 2024-07-10T12:37:08 — captured 2026-07-15

### GetMarketMessages : Returns array of last messages (Market Messages) related to selected exchange

### What is returned ?

**Market Messages as received from the Exchange**

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |

### Sample request(JavaScript)

```javascript
{
MessageType: "GetMarketMessages",
Exchange: "NFO",
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"Request":{
"Exchange":"NFO",
"MessageType":"GetMarketMessages"
},
"Result":[
{"ServerTime":1391822398,"SessionID":2,"MarketType":"Normal Market Close","MessageType":"MarketMessagesItemResult"},
```

{"ServerTime":1391822399,"SessionID":1,"MarketType":"Special Session Open","MessageType":"MarketMessagesItemResult"} ], "MessageType":"MarketMessagesResult" }

---

## GetExchangeMessages

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchangemessages-2/ — last updated 2024-07-10T12:37:02 — captured 2026-07-15

### GetExchangeMessages : Returns array of last messages (Exchange Messages) related to selected exchange

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |

### What is returned ?

**Exchange Messages as received from the Exchange**

### Sample request(JavaScript)

```javascript
var request =
{
MessageType: "GetExchangeMessages",
Exchange: "NFO",
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"Request":{
"Exchange":"NFO",
"MessageType":"GetExchangeMessages"
},
"Result":[
{"ServerTime":1391822398,"Identifier":"Market","Message":"Members are requested to note that ...","MessageType":"ExchangeMessagesItemResult"},
{"ServerTime":1391822399,"Identifier":"Market","Message":"2013 shall be levied subsequently.","MessageType":"ExchangeMessagesItemResult"}
],
"MessageType":"ExchangeMessagesResult"
}
```

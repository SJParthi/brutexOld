# GlobalDataFeeds REST API — Server Info, Account Limits & Market/Exchange Messages

Account/server utility functions of the GlobalDataFeeds REST API: GetServerInfo, GetLimitation, GetMarketMessages, GetExchangeMessages.

Sources:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getserverinfo/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlimitation/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getmarketmessages/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchangemessages/

---

## GetServerInfo

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getserverinfo/ — last updated 2024-07-03T08:23:55 — captured 2026-07-15

### GetServerInfo : Returns information about server where connection is made

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetServerInfo/?xml=true
```

**What is returned ?**

**Server EndPoint where enduser is connected (useful while debugging issues)**

**Example of returned data**

JSON:

```json
{"ServerID":"2152-24"}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<ServerInfoResult
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
ServerID="2152-24"
/>
```

CSV:

```csv
ServerID
EF89-18
```

---

## GetLimitation

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlimitation/ — last updated 2024-07-03T08:24:02 — captured 2026-07-15

### GetLimitation : Returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent. |

**Example**

```http
http://endpoint:port/GetLimitation/?accessKey=0a0b0c&xml=true
```

**What is returned ?**

**Returns details about user account (what is allowed / disallowed)**

**Example of returned data**

JSON:

```json
{"AllowedBandwidthPerHour":-1,
"AllowedCallsPerHour":-1,
"AllowedCallsPerMonth":-1,
"AllowedBandwidthPerMonth":-1,
"ExpirationDate":1485468000000,
"Enabled":true,
"AllowedExchanges":[{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"DUMMY"},{"AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NFO"}], "AllowedFunctions":[{"FunctionName":"GetExchangeMessages","IsEnabled":false},{"FunctionName":"GetHistory","IsEnabled":true}], "HistoryLimitation":{"TickEnabled":true,"DayEnabled":true,"MaxEOD":100000,"MaxIntraday":5,"MaxTicks":5,"Hour_1Enabled":true, "Hour_2Enabled":true,"Hour_4Enabled":true, "Minute_1Enabled":true,"Minute_5Enabled":true,"Minute_10Enabled":true}}
```

XML (reproduced as published; note the `<MaxTicks>5</MaxIntraday>` closing-tag mismatch and the trailing space in `</LimitationInfoResult >` appear in the original page):

```xml
<?xml version="1.0" encoding="utf-16" ?>
<LimitationInfoResult
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<AllowedBandwidthPerHour>-1</AllowedBandwidthPerHour>
<AllowedCallsPerHour>-1</AllowedCallsPerHour>
<AllowedCallsPerMonth>-1</AllowedCallsPerMonth>
<AllowedBandwidthPerMonth>-1</AllowedBandwidthPerMonth>
<ExpirationDate>"01-01-0001 12:00:00 AM"</ExpirationDate>
<Enabled>true</Enabled>
<AllowedExchanges>
<AllowedInstruments>-1</AllowedInstruments>
<DataDelay>0</DataDelay>
<ExchangeName>DUMMY</ExchangeName>
</AllowedExchanges>
<AllowedExchanges>
<AllowedInstruments>-1</AllowedInstruments>
<DataDelay>0</DataDelay>
<ExchangeName>NFO</ExchangeName>
</AllowedExchanges>
<AllowedFunctions>
<IsEnabled>false</IsEnabled>
</AllowedFunctions>
<AllowedFunctions>
<FunctionName>GetHistory</FunctionName>
<IsEnabled>true</IsEnabled>
</AllowedFunctions>
<HistoryLimitation>
<TickEnabled>true</TickEnabled>
<DayEnabled>true</DayEnabled>
<MaxEOD>100000</MaxEOD>
<MaxIntraday>5</MaxIntraday>
<MaxTicks>5</MaxIntraday>
<Hour_1Enabled>true</Hour_1Enabled>
<Hour_2Enabled>true</Hour_2Enabled>
<Hour_4Enabled>true</Hour_4Enabled>
<Minute_1Enabled>true</Minute_1Enabled>
<Minute_5Enabled>true</Minute_5Enabled>
<Minute_10Enabled>true</Minute_10Enabled>
</HistoryLimitation>
</LimitationInfoResult >
```

CSV:

```csv
AllowedBandwidthPerHour,AllowedCallsPerHour,AllowedBandwidthPerMonth,AllowedCallsPerMonth,Enabled,AllowedExchanges,AllowedFunctions,ExpirationDate,HistoryLimitation,SubscribeSnapshotLimitation,GetExchangeSnapshotLimitation,GetExchangeSnapshotInstrumentTypeLimitation,GetSnapshotLimitation
-1,-1,-1,-1,True,System.Collections.Generic.List`1[IConnectorPlugin.DataAccess.LimitationInfo.ExchangePermissionsParams],System.Collections.Generic.List`1[IConnectorPlugin.DataAccess.LimitationInfo.FunctionPermissionsParams],1719772199000,IConnectorPlugin.DataAccess.LimitationInfo.HistoryLimitationParams,,IConnectorPlugin.DataAccess.LimitationInfo.GetSnapshotLimitationParams,,
```

---

## GetMarketMessages

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getmarketmessages/ — last updated 2024-07-03T08:24:09 — captured 2026-07-15

### GetMarketMessages : Returns array of last messages (Market Messages) related to selected exchange

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |

**Example**

```http
http://endpoint:port/GetMarketMessages/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**Market Messages as received from the Exchange**

**Example of returned data**

JSON:

```json
{"EXCHANGE": "MCX","MESSAGES": [{ "SESSIONID": 2, "MARKETTYPE": "Normal Market Close", "SERVERTIME": 1391824800},{ "SESSIONID": 1, "MARKETTYPE": "Special Session Open", "SERVERTIME": 1391852700}]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<MarketMessageHistory
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
Exchange="MCX">
<Value ServerTime="2-8-2014 2:00:00 AM" SessionID="2" MarketType="Normal Market Close"/>
<Value ServerTime="2-8-2014 9:45:00 AM" SessionID="1" MarketType="Special Session Open"/>
</MarketMessageHistory>
```

CSV:

```csv
SessionID,MarketType,ServerTime
0,Normal Market Open,1713843900000
```

---

## GetExchangeMessages

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchangemessages/ — last updated 2024-07-03T08:24:17 — captured 2026-07-15

### GetExchangeMessages : Returns array of last messages (Exchange Messages) related to selected exchange

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetExchangeMessages/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**Exchange Messages as received from the Exchange**

**Example of returned data**

JSON:

```json
{"EXCHANGE": "MCX","MESSAGES": [{ "IDENTIFIER": "Market", "SERVERTIME": 1391822398, "MESSAGE": "Members are requested to note that …"},{ "IDENTIFIER": "Market", "SERVERTIME": 1391822399, "MESSAGE": "2013 shall be levied subsequently."}]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<ExchangeMessageHistory
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
Exchange="MCX">
<Value ServerTime="2-8-2014 1:19:58 AM" Identifier="Market">
<Message>
<![CDATA[ Members are requested to note that … ]]>
</Message>
</Value>
<Value ServerTime="2-8-2014 1:19:59 AM" Identifier="Market">
<Message>
<![CDATA[ 2013 shall be levied subsequently. ]]>
</Message>
</Value>
</ExchangeMessageHistory>
```

CSV:

```csv
Exchange,Identifier,ServerTime,Message
MCX,Market,1391822398,Members are requested to note that …
MCX,Market,1391822399,2013 shall be levied subsequently.
```

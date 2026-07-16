# GlobalDataFeeds GainersLosers & VolumeShockers API

Top N Gainers/Losers and Volume Shockers data functions (WebSocket and REST).

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers/subscribetopgainerslosers/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers/gettopgainerslosers/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/volume-shockers/getvolumeshockers-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers-rest-api-documentation/gettopgainerslosers-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/volume-shockers-rest-api-documentation/getvolumeshockers/

---

## SubscribeTopGainersLosers (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers/subscribetopgainerslosers/ — last updated 2025-09-05T15:04:03 — captured 2026-07-15

SubscribeTopGainersLosers : Returns data of top N gainers and losers for requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Count** | *Value like* 5, 10, 15, 20. | Mandatory parameter. Supported values: 5, 10, 15, 20 ,to receive count of Top N of Gainers and Top N of Losers |
| **Series** | *String value like* EQ | Optional parameter. User can filter the Gainers Losers by series filter. |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. Buy default subscribes to Realtime data. If [true] Product is unsubscribed. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier (Symbol), LastTradeTime, ServerTime**, **AverageTradedPrice, BuyPrice, BuyQty, Open, High, Low, Close, LastTradePrice, LastTradeQty, OpenInterest,  QuotationLot, SellPrice, SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage, OpenInterestChange, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function SubscribeTopGainersLosers()
{	
     var request = 	 
		{
			MessageType: "SubscribeTopGainersLosers",
			Exchange: "NFO",	
			Series:"EQ",	
			Count: 10,                     		
		        //Unsubscribe: false                  
		};
     doSend(request);
}
```

**Example of returned data in JSON format.**

[Download SubscribeTopGainersLosers Response](https://globaldatafeeds.in/resources/SubscribeTopGainersLosers_SampleResponse.zip)

---

## GetTopGainersLosers (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers/gettopgainerslosers/ — last updated 2025-09-05T15:04:50 — captured 2026-07-15

GetTopGainersLosers : Returns data of top N gainers and losers for requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Count** | *Value like* 5, 10, 15, 20. | Mandatory parameter. Supported values: 5, 10, 15, 20 ,to receive count of Top N of Gainers and Top N of Losers |
| **Series** | *String value like* EQ | Optional parameter. User can filter the Gainers Losers by series filter. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier (Symbol), LastTradeTime, ServerTime**, **AverageTradedPrice, BuyPrice, BuyQty, Open, High, Low, Close, LastTradePrice, LastTradeQty, OpenInterest,  QuotationLot, SellPrice, SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage, OpenInterestChange, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function GetTopGainersLosers()
{	
     var request = 	 
		{
			MessageType: "GetTopGainersLosers",
			Exchange: "NFO",					
			Count: 10                 
		};
     doSend(request);
}
```

**Example of returned data in JSON format.**

[Download GetTopGainersLosers Response](https://globaldatafeeds.in/resources/GetTopGainersLosers_SampleResponse.zip)

---

## GetVolumeShockers (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/volume-shockers/getvolumeshockers-2/ — last updated 2026-05-04T16:05:11 — captured 2026-07-15

### GetVolumeShockers : Returns data of stocks showing an unusually high trading volume compared to their 5-day average volume (TTQ > SMA(TTQ,5)), indicating sudden market activity or increased investor interest.

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NSE* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **Count** | *Value like* 5, 10, 15, 20. | Mandatory parameter. Supported values: 5, 10, 15, 20 ,to receive count of  N of volume shockers |
| **Series** | *String value like* EQ | Optional parameter. User can filter the Volume Shockers by series filter. |

**What is returned ?**

| |
| --- |
| **InstrumentIdentifier, Description , LastTradeTime, TTQ, TTQ1Week, TTQChangeTimes, LastTradePrice, PriceChange, PriceChangePercentage** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function GetVolumeShockers()
{
var request = 	 
	{
	MessageType: "GetVolumeShockers",
	Exchange: "NSE",
	Count: 5,
	Series:"EQ"
	};
     doSend(request);
}
```

**Example of returned data**

```json
[
{
"InstrumentIdentifier":"BANK10ADD",
"Description":"DSPAMCBANK10ADD",
"LastTradeTime":1777888774,
"TTQ":47545921,
"TTQ1Week":28627,
"TTQChangeTimes":1660.88,
"LastTradePrice":16.26,
"PriceChange":-0.1,
"PriceChangePercentage":-0.61
},
{
"InstrumentIdentifier":"SIS","Description":"SIS LIMITED",
"LastTradeTime":1777890176,
"TTQ":3632313,
"TTQ1Week":56101,
"TTQChangeTimes":64.75,
"LastTradePrice":400.5,
"PriceChange":45.05,
"PriceChangePercentage":12.67
},
{
"InstrumentIdentifier":"BOMDYEING",
"Description":"BOMBAY DYEING & MFG. CO L",
"LastTradeTime":1777889461,
"TTQ":26924507,
"TTQ1Week":465696,
"TTQChangeTimes":57.82,
"LastTradePrice":126.08,
"PriceChange":13.02,
"PriceChangePercentage":11.52
},
{
"InstrumentIdentifier":"EBANKNIFTY","Description":"EDELAMC - EBANKNIFTY",
"LastTradeTime":1777888229,
"TTQ":368572,
"TTQ1Week":6692,
"TTQChangeTimes":55.08,
"LastTradePrice":55.33,
"PriceChange":0.01,
"PriceChangePercentage":0.02
},
{
"InstrumentIdentifier":"LALPATHLAB",
"Description":"DR. LAL PATH LABS LTD.",
"LastTradeTime":1777888792,
"TTQ":8279962,
"TTQ1Week":241003,
"TTQChangeTimes":34.36,
"LastTradePrice":1565.7,
"PriceChange":198.5,
"PriceChangePercentage":14.52
}
],"MessageType":"LastRealtimeVolumeShockersResult"}
```

---

## GetTopGainersLosers (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/gainers-losers-rest-api-documentation/gettopgainerslosers-2/ — last updated 2025-06-11T12:29:15 — captured 2026-07-15

GetTopGainersLosers : Returns data of top N gainers and losers for requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **Count** | *Value like* 5, 10, 15, 20. | Mandatory parameter. Supported values: 5, 10, 15, 20 ,to receive count of Top N of Gainers and Top N of Losers |
| **Series** | *String value like* EQ | Optional parameter. User can filter the Gainers Losers by series filter. |
| **XML** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **Format** | CSV | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **Example** | [https://endpoint:port/GetTopGainersLosers/?accessKey=a0b0c0&exchange=NSE&count=5&Series=EQ&xml=true](https://endpoint:port/GetTopGainersLosers/?accessKey=a0b0c0&exchange=NSE&count=5&xml=true) | |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier (Symbol), LastTradeTime, ServerTime**, **AverageTradedPrice, BuyPrice, BuyQty, Open, High, Low, Close, LastTradePrice, LastTradeQty, OpenInterest,  QuotationLot, SellPrice, SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage, OpenInterestChange, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data**

| | |
| --- | --- |
| **JSON** | [Download Sample Response in JSON](https://globaldatafeeds.in/resources/_resources/GFDL_GetTopGainersLosers_json.zip) |
| **CSV** | [Download Sample Response in CSV](https://globaldatafeeds.in/resources/_resources/GFDL_GetTopGainersLosers_csv.zip) |
| **XML** | [Download Sample Response in XML](https://globaldatafeeds.in/resources/_resources/GFDL_GetTopGainersLosers_xml.zip) |

---

## GetVolumeShockers (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/volume-shockers-rest-api-documentation/getvolumeshockers/ — last updated 2025-10-23T12:48:47 — captured 2026-07-15

### GetVolumeShockers : Returns data of stocks showing an unusually high trading volume compared to their 5-day average volume (TTQ > SMA(TTQ,5)), indicating sudden market activity or increased investor interest.

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NSE* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **Count** | *Value like* 5, 10, 15, 20. | Mandatory parameter. Supported values: 5, 10, 15, 20 ,to receive count of  N of volume shockers |
| **Series** | *String value like* EQ | Optional parameter. User can filter the Volume Shockers by series filter. |
| **XML** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **Format** | CSV | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **Example** | [https://endpoint:port/GetVolumeShockers?accessKey=Your_API_Key&exchange=NSE&](https://endpoint:port/GetVolumeShockers?accessKey=Your_API_Key&exchange=NSE&count=5&series=EQ&xml=false&format=Json) [count=5&series=EQ&format=Json](https://endpoint:port/GetVolumeShockers?accessKey=Your_API_Key&exchange=NSE&count=5&series=EQ&xml=false&format=Json) | |

**What is returned ?**

| |
| --- |
| **InstrumentIdentifier, Description , LastTradeTime, TTQ, TTQ1Week, TTQChangeTimes, LastTradePrice, PriceChange, PriceChangePercentage** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Example of returned data**

**JSON**

```json
{ "Value": [ { "InstrumentIdentifier": "VTL", "Description": "Vardhman Textiles Limted", "LastTradeTime": 1761202709000, "TTQ": "9729121", "TTQ1Week": 114066, "TTQChangeTimes": 85.29, "LastTradePrice": 446.15, "PriceChange": 37.9, "PriceChangePercentage": 9.28 }, { "InstrumentIdentifier": "EPACKPEB", "Description": "EPack Prefab Techn Ltd", "LastTradeTime": 1761202701000, "TTQ": "22911766", "TTQ1Week": 505686, "TTQChangeTimes": 45.31, "LastTradePrice": 234.3, "PriceChange": 30.86, "PriceChangePercentage": 15.17 }, { "InstrumentIdentifier": "COASTCORP", "Description": "Coastal Corporation Limited", "LastTradeTime": 1761202333000, "TTQ": "3399116", "TTQ1Week": 84375, "TTQChangeTimes": 40.29, "LastTradePrice": 44.8, "PriceChange": 7.46, "PriceChangePercentage": 19.98 }, { "InstrumentIdentifier": "GULPOLY", "Description": "Gulshan Polyols Ltd.", "LastTradeTime": 1761202670000, "TTQ": "1631328", "TTQ1Week": 42665, "TTQChangeTimes": 38.24, "LastTradePrice": 154.43, "PriceChange": 12.74, "PriceChangePercentage": 8.99 }, { "InstrumentIdentifier": "BSOFT", "Description": "BIRLASOFT LIMITED", "LastTradeTime": 1761202708000, "TTQ": "20911949", "TTQ1Week": 697151, "TTQChangeTimes": 30, "LastTradePrice": 380.85, "PriceChange": 29.8, "PriceChangePercentage": 8.49 } ] }
```

**XML**

```xml
<VolumeShockers>
<Value>
<VolumeShockersItem InstrumentIdentifier="VTL" Description="Vardhman Textiles Limted" LastTradeTime="1761202757000″ TTQ="9738497″ TTQ1Week="114066″ TTQChangeTimes="85.38″ LastTradePrice="446.1″ PriceChange="37.85″ PriceChangePercentage="9.27″/>
<VolumeShockersItem InstrumentIdentifier="EPACKPEB" Description="EPack Prefab Techn Ltd" LastTradeTime="1761202756000″ TTQ="22950604″ TTQ1Week="505686″ TTQChangeTimes="45.39″ LastTradePrice="234″ PriceChange="30.56″ PriceChangePercentage="15.02″/>
<VolumeShockersItem InstrumentIdentifier="COASTCORP" Description="Coastal Corporation Limited" LastTradeTime="1761202333000″ TTQ="3399116″ TTQ1Week="84375″ TTQChangeTimes="40.29″ LastTradePrice="44.8″ PriceChange="7.46″ PriceChangePercentage="19.98″/>
<VolumeShockersItem InstrumentIdentifier="GULPOLY" Description="Gulshan Polyols Ltd." LastTradeTime="1761202715000″ TTQ="1631929″ TTQ1Week="42665″ TTQChangeTimes="38.25″ LastTradePrice="154.06″ PriceChange="12.37″ PriceChangePercentage="8.73″/>
<VolumeShockersItem InstrumentIdentifier="BSOFT" Description="BIRLASOFT LIMITED" LastTradeTime="1761202752000″ TTQ="21064968″ TTQ1Week="697151″ TTQChangeTimes="30.22″ LastTradePrice="379″ PriceChange="27.95″ PriceChangePercentage="7.96″/>
</Value>
</VolumeShockers>
```

**CSV**

```csv
InstrumentIdentifier,Description ,LastTradeTime,TTQ,TTQ1Week,TTQChangeTimes,LastTradePrice,PriceChange,PriceChangePercentage VTL,Vardhman Textiles Limted,1761202783000,9743960,114066,85.42,446.2,37.95,9.3 EPACKPEB,EPack Prefab Techn Ltd,1761202756000,22950604,505686,45.39,234.0,30.56,15.02 COASTCORP,Coastal Corporation Limited,1761202333000,3399116,84375,40.29,44.8,7.46,19.98 GULPOLY,Gulshan Polyols Ltd.,1761202759000,1633547,42665,38.29,153.8,12.11,8.55 BSOFT,BIRLASOFT LIMITED,1761202784000,21113720,697151,30.29,379.45,28.4,8.09
```

# GlobalDataFeeds Greeks API (WebSocket)

WebSocket Greeks API — realtime, snapshot, last-quote, option-chain and historical Option Greeks (IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR).

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscriberealtimegreeks/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscribesnapshotgreeks/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getsnapshotgreeks-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscribeoptiongreekschain/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreeks/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquotearrayoptiongreeks/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreekschain/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/gethistorygreeks-2/

---

## SubscribeRealtimeGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscriberealtimegreeks/ — last updated 2026-05-04T15:02:39 — captured 2026-07-15

### SubscribeRealtimeGreeks: Subscribe to Realtime Greek values of single Token, returns market data every second (detailed)

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Token** | *Token numbers of instruments* | How to get list of available token numbers of instruments you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |

**What is returned ?**

**Exchange, Token(TokenNumber of Symbol), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR**

**Sample request(JavaScript)**

```javascript
{
MessageType: "SubscribeRealtimeGreeks",
Exchange: "NFO",
Token: "74241"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format. This data is returned every second**

```json
{
"Exchange":"NFO",
"Token":"74241",
"Timestamp":1777886925,
"IV":0.3333652913570404,
"Delta":0.00890996865928173,
"Theta":-1.2999999523162842,
"Vega":0.31678643822669983,
"Gamma":5.496752419276163E-05,
"IVVwap":0.27997586131095886,
"Vanna":0.0018198598409071565,
"Charm":-0.00890996865928173,
"Speed":2.954922422304662E-07,
"Zomma":7.80598111305153E-06,
"Color":-5.496752419276163E-05,
"Volga":0.05583925172686577,
"Veta":-0.31678643822669983,
"ThetaGammaRatio":-23650.328125,
"ThetaVegaRatio":-4.103711128234863,
"DTR":-0.006853822618722916,
"MessageType":"RealtimeGreeksResult""
}
```

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## SubscribeSnapshotGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscribesnapshotgreeks/ — last updated 2026-05-04T15:10:27 — captured 2026-07-15

### SubscribeSnapshotGreeks: Subscribe to Realtime Greeks Snapshots data, returns data snapshot as per Periodicity & Period values

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier like NIFTY30SEP2525000CE* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *MINUTE* | String value of required periodicity. |
| **Period** | *1* | Numerical value of required Period. |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. By default subscribes to Realtime Snapshot Greeks data. If set to [true], the specified instrumentIdentifier will be unsubscribed, meaning the data stream for the subscribed instrument(s) will stop. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier, Periodicity, Period, LastTradeTime, Token, IV, Delta, Theta, Vega,** **Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function SubscribeSnapshotGreeks()
{
var request =
{
MessageType: "SubscribeSnapshotGreeks",
Exchange: "NFO",
Periodicity: "Minute",
Period: 1,
Unsubscribe: false,
InstrumentIdentifier:"NIFTY26MAY2624100CE",
isShortIdentifiers: "true"
// Example of InstrumentIdentifier: OPTIDX_NIFTY_30SEP2025_CE_25000,
NIFTY30SEP2525000CE
};
doSend(request);
}
```

**Example of returned data in JSON format. This data returned as per Periodicity & Period values**

```json
{"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY26MAY2624100CE",
"Periodicity":"MINUTE",
"Period":1,
"LastTradeTime":1777887420,
"Token":"72183",
"IV":0.17984823882579803,
"Delta":0.5460793972015381,
"Theta":-9.610696792602539,
"Vega":23.57541847229004,
"Gamma":0.0003703689726535231,
"IVVwap":0.17672587931156158,
"Vanna":-0.001468126429244876,
"Charm":0.0006700361846014857,
"Speed":-5.553241422262545E-08,
"Zomma":-1.9360619262442924E-05,
"Color":8.616986633569468E06,
"Volga":0.009949108585715294,
"Veta":-0.5451173782348633,
"ThetaGammaRatio":-25948.978515625,
"ThetaVegaRatio":-0.40765753388404846,
"DTR":-0.05681996047496796,
"MessageType":"RealtimeSnapshotGreeksResult"}
```

---

## GetSnapshotGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getsnapshotgreeks-2/ — last updated 2026-05-04T15:17:53 — captured 2026-07-15

GetSnapshotGreeks : Returns latest Snapshot Greeks Data of multiple Symbols – max 25 in single call

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like* NFO | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Periodicity** | *MINUTE* | String value of required periodicity. |
| **Period** | *Numerical value 1, default = 1* | Optional parameter of required period for historical data. Can be applied for [MINUTE] periodicity types |
| **InstrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier, Periodicity, Period, LastTradeTime, Token, IV, Delta, Theta, Vega,** **Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
            {
                MessageType: "GetSnapshotGreeks",
                Exchange: "NFO",
                Periodicity: "MINUTE",
                Period: 1,
		isShortIdentifiers: "true",
                InstrumentIdentifiers: [{Value:"NIFTY26MAY2624000CE"}, {Value:"BANKNIFTY26MAY2654900CE"}]
            };
     doSend(request);
```

**Example of returned data in JSON format**

```json
[
{
"InstrumentIdentifier":"BANKNIFTY26MAY2654900CE",
"Exchange":"NFO",
"LastTradeTime":1777887780,
"TokenNumber":"67257",
"IV":0.2034873515367508,
"Delta":0.5487319231033325,
"Theta":-24.778276443481445,
"Vega":53.68198013305664,
"Gamma":0.0001435296144336462,
"IVVwap":0.20431892573833466,
"Vanna":-0.0013212690828368068,
"Charm":0.0006790192564949393,
"Speed":-8.981875687652519E-09,
"Zomma":-6.667955403827364E-06,
"Color":3.3397886909369845E-06,
"Volga":0.02163698710501194,
"Veta":-1.2429059743881226,
"ThetaGammaRatio":-172635.296875,
"ThetaVegaRatio":-0.46157532930374146,
"DTR":-0.02214568480849266
},
{
"InstrumentIdentifier":"NIFTY26MAY2624000CE",
"Exchange":"NFO",
"LastTradeTime":1777887780,
"TokenNumber":"72179",
"IV":0.1822797656059265,
"Delta":0.5858505964279175,
"Theta":-9.58560562133789,
"Vega":23.183399200439453,
"Gamma":0.0003593154833652079,
"IVVwap":0.17859512567520142,
"Vanna":-0.0034659092780202627,
"Charm":0.0015788835007697344,
"Speed":-8.684147445592316E-08,
"Zomma":-1.8043487216345966E-05,
"Color":8.11245718068676E-06,
"Volga":0.043806031346321106,
"Veta":-0.5520684719085693,
"ThetaGammaRatio":-26677.40625,
"ThetaVegaRatio":-0.41346850991249084,
"DTR":-0.0611177459359169
}
],
"MessageType":"SnapshotGreeksResult"}
```

**Important Note:**

GetSnapshotGreeks , the time when request is sent is very important. For example : If user sends request for 1minute snapshot data at 9:20:01, he will get record of trade data between 9:19:00 to 9:20:00. However, if request is sent at 9:19:58 i.e. before minute is complete, user will get trade data between 9:18:00 to 9:19:00. In both cases, timestamp of the returned data will be start timestamp of the period. So when data between 9:15:00 to 9:16:00 is returned, it will have timestamp of 9:15:00. Also if a user subscribes for snapshot data of any symbol for a period and if no trades happen during that period, no data is sent by the server.
Also, this function works only during market hours.

---

## SubscribeOptionChainGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscribeoptiongreekschain/ — last updated 2025-03-06T14:20:36 — captured 2026-07-15

SubscribeOptionChainGreeks : Returns data of entire OptionChain Greeks for requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Product** | *String value like BANKNIFTY* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value of expiry in DDMMYYYY format, like 23JAN2020* | Mandatory parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. If absent, result is sent for all Option Types. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |
| **StrikePrice** | *Value like 10000, 75.5, etc.* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) |
| **Depth** | *Value like* 1, 5, 10, 15, 20. | Mandatory parameter. To received count value up and down from "StrikePrice" |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. Buy default subscribes to Realtime data. If [true] Product is unsubscribed. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier (Symbol), LastTradeTime, AverageTradedPrice, BuyPrice, BuyQty, Open, High, Low, Close, LastTradePrice, LastTradeQty, OpenInterest,  QuotationLot, SellPrice, SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage, OpenInterestChange, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function SubscribeOptionChainGreeks()
{	
     var request = 	 
		{
			MessageType: "SubscribeOptionChainGreeks",
			Exchange: "NFO",					
			Product: "NIFTY",					
			Expiry:"17OCT2024",					
			//OptionType:"PE",					
			StrikePrice: 25000, 				
			Depth: 5 							
		        //Unsubscribe: false                  
		};
     doSend(request);
}
```

**Example of returned data in JSON format.**

[Download SubscribeOptionChainGreeks Sample Response](https://globaldatafeeds.in/resources/_resources/SubscribeOptionChainGreeks_Response.zip)

---

## GetLastQuoteOptionGreek (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreeks/ — last updated 2026-05-04T15:23:02 — captured 2026-07-15

### GetLastQuoteOptionGreek : Returns Last Traded Option Greek values of Single Symbol (detailed)

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Mandatory parameter.Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Token** | *Token number of instrument* | How to get list of available token numbers of instruments you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |

**What is returned ?**

**Exchange, Token(TokenNumber of Symbol), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR**

**Sample request(JavaScript)**

```javascript
{
MessageType: "GetLastQuoteOptionGreeks",
Exchange: "NFO",
Token: "74241"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format**

```json
{
"Exchange":"NFO",
"Token":"74241",
"Timestamp":1777888199,
"IV":0.3248942792415619,
"Delta":0.008018498308956623,
"Theta":-1.100000023841858,
"Vega":0.2815832793712616,
"Gamma":5.273253918858245E-05,
"IVVwap":0.28445038199424744,
"Vanna":0.001736388192512095,
"Charm":-0.008018498308956623,
"Speed":3.0339859335981595E-07,
"Zomma":8.013773367565591E-06,
"Color":-5.273253918858245E-05,
"Volga":0.05277629569172859,
"Veta":-0.2815832793712616,
"ThetaGammaRatio":-20859.986328125,
"ThetaVegaRatio":-3.906481981277466,
"DTR":-0.0072895437479019165,
"MessageType":"LastQuoteOptionGreeksResult"
}
```

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## GetLastQuoteArrayOptionGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquotearrayoptiongreeks/ — last updated 2026-05-04T15:50:33 — captured 2026-07-15

### GetLastQuoteArrayOptionGreeks : Returns Last Traded Option Greek values of multiple Symbols – max 25 in single call (detailed)

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Mandatory parameter. Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Tokens** | *Token numbers of instruments* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |

**What is returned ?**

**Exchange, Token(TokenNumber of Symbol), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR**

**Sample request(JavaScript)**

```javascript
{
MessageType: "GetLastQuoteOptionGreeks",
Exchange: "NFO",
Tokens: [{Value:"74817"},{Value:"74104"}]
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format**

```json
[
{"Exchange":"NFO",
"Token":"74817",
"Timestamp":1771915984,
"IV":1.7473117113113403,
"Delta":-0.002514060353860259,
"Theta":-0.05000000074505806,
"Vega":0.002931205090135336,
"Gamma":0.00011879863450303674,
"IVVwap":1.293108582496643,
"Vanna":-0.00012433831579983234,
"Charm":0.0,
"Speed":-5.091221282782499E-06,
"Zomma":4.591108336171601E-06,
"Color":0.0,"Volga":0.0001307035709032789,
"Veta":0.0,
"ThetaGammaRatio":-420.8802490234375,
"ThetaVegaRatio":-17.057830810546875,
"DTR":0.05028120800852776,
"MessageType":"LastQuoteOptionGreeksResult"},
{"Exchange":"NFO",
"Token":"74104",
"Timestamp":1777888799,
"IV":0.24014639854431152,
"Delta":-0.015239386819303036,
"Theta":-1.649999976158142,
"Vega":0.4851972162723541,
"Gamma":0.00012648034316953272,
"IVVwap":0.24263548851013184,
"Vanna":-0.0036234345752745862,
"Charm":0.015239386819303036,
"Speed":-9.043463933267049E-07,
"Zomma":1.923513991641812E-05,
"Color":-0.00012648034316953272,
"Volga":0.0970657244324684,
"Veta":-0.4851972162723541,
"ThetaGammaRatio":-13045.505859375,
"ThetaVegaRatio":-3.400678873062134,
"DTR":0.009235992096364498,
"MessageType":"LastQuoteOptionGreeksResult"}],
"MessageType":"LastQuoteArrayOptionGreeksResult"}
```

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## GetLastQuoteOptionGreeksChain (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreekschain/ — last updated 2025-09-27T14:24:26 — captured 2026-07-15

### GetLastQuoteOptionGreeksChain : Returns Last Traded Option Greek values of entire OptionChain of requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Product** | *String value like BANKNIFTY* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value of expiry in DDMMYYYY format, like 29JUL2021* | Optional parameter. Name of supported Expiry Date. If absent, result is sent for all active expiries. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. If absent, result is sent for all Option Types. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |
| **StrikePrice** | *Value like 10000, 75.5, etc* | Optional parameter. If absent, result is sent for all Strike Prices. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) |

**What is returned ?**

**Exchange**,InstrumentIdentifier (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice**,(VWAP), **Close** (previous Day's Close), Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close), Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR

**Sample request(JavaScript)**

```javascript
{
MessageType: "GetLastQuoteOptionGreeksChain",
Exchange: "NFO",
Product: "NIFTY"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format**

[Download](https://globaldatafeeds.in/resources/OptionGreeksChainWithQuoteResponse_JSON.zip)

**FAQ**

We are receiving these values directly from NSE. Here are some FAQs which you may find useful :

1. Which model is used to compute Implied Volatility ? Is it Black Scholes or Black 76 ?

Response: Black Scholes Model

2. If it is Black Scholes model, what is the interest rate assumed ?

Response: Black Scholes Model, underlying, is taken as Futures or Synthetic futures( in case of expiries where future is not available). Thus Spot Price and Interest rate as a parameter gets removed

---

## GetHistoryGreeks (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/gethistorygreeks-2/ — last updated 2025-12-10T10:55:47 — captured 2026-07-15

GetHistoryGreeks : Returns historical greeks data

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like* NFO | Name of supported exchange. |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *MINUTE, TICK* | String value of required periodicity. |
| **Period** | *Numerical value 1,  (default = 1*) | Optional parameter of required period for historical data. Can be applied for [MINUTE]/[TICK] periodicity types |
| **From** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter. It means starting timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **To** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter. It means ending timestamp for called historical data. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **Max** | *Numerical value. Default = 0 (means all available data)* | Optional parameter. It limits returned data. If Max = N, N records will be received in the response. |
| **userTag** | *Any string value* | Optional parameter. It will be returned with historical data. |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY28NOV2426050CE, etc. |

**What is returned ?**

| |
| --- |
| **Token, Timestamp, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR.** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
{
                MessageType: "GetHistoryGreeks",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY28NOV2426050CE",
		isShortIdentifier: "TRUE",
                Max: 5
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format**

```text
RESPONSE: {"Request":{"Exchange":"NFO","InstrumentIdentifier":"NIFTY28NOV2426050CE","IsShortIdentifier":true,"From":0,"To":0,"Max":5,"UserTag":null,"MessageType":"GetHistoryGreeks"},"Result":[{"Token":"48093","Timestamp":1731999380,"IV":0.22,"Delta":0.01,"Theta":-0.77,"Vega":0.66,"Gamma":0.0,"IVVwap":0.22,"Vanna":0.0,"Charm":-0.0,"Speed":0.0,"Zomma":0.0,"Color":-0.0,"Volga":0.2,"Veta":-0.23,"ThetaGammaRatio":-38028.19,"ThetaVegaRatio":-1.17,"DTR":-0.01},{"Token":"48093","Timestamp":1731999294,"IV":0.22,"Delta":0.01,"Theta":-0.79,"Vega":0.68,"Gamma":0.0,"IVVwap":0.22,"Vanna":0.0,"Charm":-0.0,"Speed":0.0,"Zomma":0.0,"Color":-0.0,"Volga":0.21,"Veta":-0.24,"ThetaGammaRatio":-38031.55,"ThetaVegaRatio":-1.17,"DTR":-0.01},{"Token":"48093","Timestamp":1731999290,"IV":0.22,"Delta":0.01,"Theta":-0.79,"Vega":0.68,"Gamma":0.0,"IVVwap":0.22,"Vanna":0.0,"Charm":-0.0,"Speed":0.0,"Zomma":0.0,"Color":-0.0,"Volga":0.21,"Veta":-0.24,"ThetaGammaRatio":-38030.77,"ThetaVegaRatio":-1.17,"DTR":-0.01},{"Token":"48093","Timestamp":1731999209,"IV":0.22,"Delta":0.01,"Theta":-0.79,"Vega":0.68,"Gamma":0.0,"IVVwap":0.22,"Vanna":0.0,"Charm":-0.0,"Speed":0.0,"Zomma":0.0,"Color":-0.0,"Volga":0.21,"Veta":-0.24,"ThetaGammaRatio":-37929.38,"ThetaVegaRatio":-1.16,"DTR":-0.01},{"Token":"48093","Timestamp":1731998888,"IV":0.22,"Delta":0.01,"Theta":-0.81,"Vega":0.7,"Gamma":0.0,"IVVwap":0.22,"Vanna":0.0,"Charm":-0.0,"Speed":0.0,"Zomma":0.0,"Color":-0.0,"Volga":0.21,"Veta":-0.24,"ThetaGammaRatio":-37791.3,"ThetaVegaRatio":-1.16,"DTR":-0.01}],"MessageType":"HistoryGreeksResult"}
```

# GlobalDataFeeds OptionChain API

OptionChain API — subscribe to / fetch the entire OptionChain of a requested underlying, via WebSocket and REST.

Sources:
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api/subscribeoptionchain/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api/function-getlastquoteoptionchain/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api-rest-api-documentation/getlastquoteoptionchain/

---

## SubscribeOptionChain (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api/subscribeoptionchain/ — last updated 2025-01-07T12:51:18 — captured 2026-07-15

SubscribeOptionChain : Returns data of entire OptionChain for requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Product** | *String value like BANKNIFTY* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value of expiry in DDMMYYYY format, like 23JAN2020* | Mandatory parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. If absent, result is sent for all Option Types. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |
| **StrikePrice** | *Value like 10000, 75.5, etc.* | Mandatory parameter .How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) |
| **Depth** | *Value like* 1, 5, 10, 15, 20. | Mandatory parameter. To received count value up and down from "StrikePrice" |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. Buy default subscribes to Realtime data. If [true] Product is unsubscribed. |

**What is returned ?**

| |
| --- |
| **Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime**, **AverageTradedPrice, BuyPrice, BuyQty, Open, High, Low, Close, LastTradePrice, LastTradeQty, OpenInterest, OpenInterest, QuotationLot, SellPrice, SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage, OpenInterestChange, MessageType** |
| LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

**Sample request(JavaScript)**

```javascript
function SubscribeOptionChain()
{	
     var request = 	 
		{
			MessageType: "SubscribeOptionChain",
			Exchange: "NFO",					
			Product: "NIFTY",					
			Expiry:"17OCT2024",					
			// OptionType:"CE",					
			StrikePrice: 25000, 				
			Depth: 5 							
		       // Unsubscribe: false                  
		};
     doSend(request);
}
```

**Example of returned data in JSON format.**

[Download SubscribeOptionChain Response](https://globaldatafeeds.in/resources/_resources/SubscribeOptionChain_Response%281%29.zip)

---

## GetLastQuoteOptionChain (WebSocket)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api/function-getlastquoteoptionchain/ — last updated 2025-09-27T14:22:21 — captured 2026-07-15

### GetLastQuoteOptionChain : Returns LastTradePrice of entire OptionChain of requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Product** | *String value like BANKNIFTY* | Mandatory parameter. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value of expiry in DDMMYYYY format, like 23JAN2020* | Optional parameter. Name of supported Expiry Date. If absent, result is sent for all active expiries. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. If absent, result is sent for all Option Types. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |
| **StrikePrice** | *Value like 10000, 75.5, etc.* | Optional parameter. If absent, result is sent for all Strike Prices. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) |

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close

**Sample request(JavaScript)**

```javascript
{
MessageType: "GetLastQuoteOptionChain",
Exchange: "NFO",
Product: "NIFTY"
};
var message = JSON.stringify(request);
websocket.send(message);
```

**Example of returned data in JSON format**

[Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionChainResponse_JSON.zip)

---

## GetLastQuoteOptionChain (REST)

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api-rest-api-documentation/getlastquoteoptionchain/ — last updated 2026-05-04T17:26:07 — captured 2026-07-15

### GetLastQuoteOptionChain : Returns LastTradePrice of entire OptionChain of requested underlying

**Supported parameters**

| | | |
| --- | --- | --- |
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **product** | *String value like NIFTY, BANKNIFTY, etc..* | Mandatory Parameter. How to get list of available Products, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value of expiry in DDMMYYYY format, like 23JAN2020* | Optional parameter. If absent, result is sent for all active Expiries. How to get list of available Expiries, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) |
| **optionType** | *String value like CE, PE* | Optional parameter. If absent, result is sent for all OptionTypes. How to get list of available OptionTypes, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) |
| **strikePrice** | *like 10000, 75.5, etc.* | Optional parameter. If absent, result is sent for all active Strike Prices. How to get list of available Strike Prices, you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **Example** | [http://endpoint:port/GetLastQuoteOptionChain/?accessKey=0a0b0c&exchange=NFO&product=NIFTY](http://endpoint:port/GetLastQuoteOptionChain/?accessKey=0a0b0c&exchange=NFO&product=NIFTY) | |

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close),**TotalBuyQty,TotalSellQty**

**Example of returned data**

| | |
| --- | --- |
| **JSON** | **XML** |
| [Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionChainResponse_JSON.zip) | [Download](https://globaldatafeeds.in/resources/GetLastQuoteOptionChainResponse_XML.zip) |
| **CSV** | [Download](https://globaldatafeeds.in/resources/GetLastQuoteChainResponse_CSV.zip) |

# GlobalDataFeeds WebSockets API — Subscribe Functions

Realtime subscription functions of the GlobalDataFeeds WebSockets API: SubscribeRealtime (tick-by-second stream) and SubscribeSnapshot (periodic OHLC snapshots).

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscriberealtime/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscribesnapshot/

---

## SubscribeRealtime

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscriberealtime/ — last updated 2026-04-13T10:59:57 — captured 2026-07-15

### SubscribeRealtime : Subscribe to Realtime data, returns market data every second (Bid/Ask/Trade)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. Buy default subscribes to Realtime data. If [true], instrumentIdentifier is unsubscribed |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open** (Day's Open), **High** (Day's High)**, Low** (Day's Low)**, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close)

LastTradeTime, ServerTradeTime : These values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
{
MessageType: "SubscribeRealtime",
Exchange: "NFO",
InstrumentIdentifier: "NIFTY-I"
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format. This data is returned every second

```json
{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY-I",
"LastTradeTime":1776057749,
"ServerTime":1776057749,
"AverageTradedPrice":23689.38,
"BuyPrice":23772.3,
"BuyQty":325,
"Close":24101.0,
"High":23782.0,
"Low":23625.0,
"LastTradePrice":23775.0,
"LastTradeQty":0,
"Open":23748.0,
"OpenInterest":18782270,
"QuotationLot":65.0,
"SellPrice":23775.0,
"SellQty":975,
"TotalQtyTraded":2596880,
"Value":61518477134.4,
"PreOpen":false,
"PriceChange":-326.0,
"PriceChangePercentage":-1.35,
"OpenInterestChange":205335,
"MessageType":"RealtimeResult"
}
```

---

## SubscribeSnapshot

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscribesnapshot/ — last updated 2024-10-08T12:11:42 — captured 2026-07-15

### SubscribeSnapshot : Subscribe to Realtime Snapshots data, returns data snapshot as per Periodicity & Period values

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *String value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **Periodicity** | *["MINUTE"]/["HOUR"]* | String value of required periodicity. |
| **Period** | *1,2,5,10,15,30 default = 1* | Numerical value of required Period. |
| **Unsubscribe** | *[true]/[false], default = [false]* | Optional parameter. By default subscribes to Realtime data. If [true] instrumentidentfier is unsubscribed |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **Periodicity, Period, LastTradeTime, Open, High, Low, Close, TradedQty, OpenInterest**

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
{
MessageType: "SubscribeSnapshot",
Exchange: "NFO",
InstrumentIdentifier: "FUTIDX_BANKNIFTY_24NOV2016_XX_0",
Periodicity: "MINUTE",
Period: 1
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format. This data returned as per Periodicity & Period values

```json
{
"Exchange":"NFO",
"InstrumentIdentifier":"FUTIDX_BANKNIFTY_24NOV2016_XX_0",
"Periodicity":"MINUTE",
"Period":1,
"LastTradeTime":1508835184,
"TradedQty":0,
"OpenInterest":0,
"Open":20162.0,
"High":20162.0,
"Low":20162.0,
"Close":20162.0,
"MessageType":"RealtimeSnapshotResult"
}
```

# GlobalDataFeeds WebSockets API — Last Quote Functions

Last-traded-price query functions of the GlobalDataFeeds WebSockets API: GetLastQuote, GetLastQuoteShort, GetLastQuoteShortWithClose, GetLastQuoteArray, GetLastQuoteArrayShort and GetLastQuoteArrayShortWithClose.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquote-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshort/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshortwithclose/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquotearray-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshort/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshortwithclose/

---

## GetLastQuote

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquote-2/ — last updated 2026-05-04T16:31:19 — captured 2026-07-15

### GetLastQuote : Returns LastTradePrice of Single Symbol (detailed)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close) ,**TotalBuyQty , TotalSellQty**

LastTradeTime, ServerTime : These values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
{
MessageType: "GetLastQuote",
Exchange: "NSE",
InstrumentIdentifier: "SBIN",
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"Exchange":"NSE",
"InstrumentIdentifier":"SBIN",
"LastTradeTime":1777890555,
"ServerTime":1777890556,
"AverageTradedPrice":1076.63,
"BuyPrice":1068.4,
"BuyQty":4158,
"Close":1068.4,
"High":1089.1,
"Low":1063.6,
"LastTradePrice":1068.4,
"LastTradeQty":0,
"Open":1063.6,
"OpenInterest":0,
"QuotationLot":1.0,
"SellPrice":0.0,
"SellQty":0,
"TotalQtyTraded":11693211,
"Value":12589261758.93,
"PreOpen":false,
"PriceChange":-0.05,
"PriceChangePercentage":-0.0,
"OpenInterestChange":0,
"TotalBuyQty":0.0,
"TotalSellQty":0.0,
"MessageType":"LastQuoteResult"
}
```

---

## GetLastQuoteShort

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshort/ — last updated 2026-05-04T16:17:56 — captured 2026-07-15

### GetLastQuoteShort : Returns LastTradePrice of Single Symbol (short)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetLastQuoteShort",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY-I",
            };
     doSend(request);
```

### Example of returned data in JSON format

```json
{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY-I",
"LastTradeTime":1777888801,
"BuyPrice":24190.0,
"LastTradePrice":24199.0,
"SellPrice":24200.0,
"MessageType":"LastQuoteShortResult"
}
```

---

## GetLastQuoteShortWithClose

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshortwithclose/ — last updated 2026-05-04T16:34:28 — captured 2026-07-15

### GetLastQuoteShortWithClose : Returns LastTradePrice of Single Symbol (short) with Close of Previous Day

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **Close** (Previous Trading Day's Close), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetLastQuoteShortWithClose",
                Exchange: "NFO",
                InstrumentIdentifier: "NIFTY26MAY26FUT",
		isShortIdentifier: "true"
            };
     doSend(request);
```

### Example of returned data in JSON format

```json
{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY26MAY26FUT",
"LastTradeTime":1777888801,
"BuyPrice":24190.0,
"Close":24098.2,
"LastTradePrice":24199.0,
"SellPrice":24200.0,
"MessageType":"LastQuoteShortWithCloseResult"
}
```

---

## GetLastQuoteArray

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquotearray-2/ — last updated 2026-05-04T16:49:39 — captured 2026-07-15

### GetLastQuoteArray : Returns LastTradePrice of multiple Symbols – max 25 in single call (detailed)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifiers** | *Value of instrument identifiers – max 25* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close),**TotalBuyQty**,**TotalSellQty**

LastTradeTime, ServerTime : These values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetLastQuoteArray",
                Exchange: "NFO",
				isShortIdentifiers: "false",
                InstrumentIdentifiers: [{Value:"NIFTY-I"}, {Value:"-I"}, {Value:"RELIANCE-I"}]
            };
     doSend(request);
```

### Example of returned data in JSON format

```json

 {"Result":[{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY-I",
"LastTradeTime":1777888801,
"ServerTime":1777888802,
"AverageTradedPrice":24244.1,
"BuyPrice":24190.0,
"BuyQty":65,
"Close":24098.2,
"High":24381.3,
"Low":24072.0,
"LastTradePrice":24199.0,
"LastTradeQty":325,
"Open":24221.0,
"OpenInterest":15774785,
"QuotationLot":65.0,
"SellPrice":24200.0,
"SellQty":455,
"TotalQtyTraded":4780815,
"Value":115906556941.5,
"PreOpen":false,
"PriceChange":100.8,
"PriceChangePercentage":0.42,
"OpenInterestChange":545025,
"TotalBuyQty":0.0,
"TotalSellQty":0.0,
"MessageType":"LastQuoteResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"SBIN-I",
"LastTradeTime":1777888800,
"ServerTime":1777888801,
"AverageTradedPrice":1069.14,
"BuyPrice":1059.6,
"BuyQty":24750,
"Close":1064.1,
"High":1082.0,
"Low":1058.0,
"LastTradePrice":1059.8,
"LastTradeQty":0,
"Open":1062.4,
"OpenInterest":74043000,
"QuotationLot":750.0,
"SellPrice":1060.5,
"SellQty":2250,
"TotalQtyTraded":10833750,
"Value":11582795475.0,
"PreOpen":false,
"PriceChange":-4.3,
"PriceChangePercentage":-0.4,
"OpenInterestChange":3663000,
"TotalBuyQty":0.0,
"TotalSellQty":0.0,
"MessageType":"LastQuoteResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"RELIANCE-I",
"LastTradeTime":1777888799,
"ServerTime":1777888801,
"AverageTradedPrice":1457.18,
"BuyPrice":1469.7,
"BuyQty":1000,
"Close":1435.2,
"High":1470.4,
"Low":1438.8
LastTradePrice":1469.7,
"LastTradeQty":500,
"Open":1438.8,
"OpenInterest":84666500,
"QuotationLot":500.0,
"SellPrice":1469.8,
"SellQty":1000,
"TotalQtyTraded":16491500,
"Value":24031083970.0,
"PreOpen":false,
"PriceChange":34.5,
"PriceChangePercentage":2.4,
"OpenInterestChange":2401500,
"TotalBuyQty":0.0,
"TotalSellQty":0.0,
"MessageType":"LastQuoteResult"}],
"MessageType":"LastQuoteArrayResult"}
```

---

## GetLastQuoteArrayShort

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshort/ — last updated 2026-05-04T17:02:59 — captured 2026-07-15

### GetLastQuoteArrayShort : Returns LastTradePrice of multiple Symbols – max 25 in single call (short)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifiers** | *Value of instrument identifiers – max 25* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetLastQuoteArrayShort",
                Exchange: "NFO",
				isShortIdentifiers: "true",
                InstrumentIdentifiers:[{Value:"NIFTY26MAY26FUT"}, {Value:"BANKNIFTY26MAY26FUT"}, {Value:"RELIANCE26MAY26FUT"}],
            };
     doSend(request);
```

### Example of returned data in JSON format

```json
{"Result":[{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY26MAY26FUT",
"LastTradeTime":1777888801,
"BuyPrice":24190.0,
"LastTradePrice":24199.0,
"SellPrice":24200.0,
"MessageType":"LastQuoteShortResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"BANKNIFTY26MAY26FUT",
"LastTradeTime":1777888799
,"BuyPrice":55100.0,
"LastTradePrice":55100.0,
"SellPrice":55130.0,
"MessageType":"LastQuoteShortResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"RELIANCE26MAY26FUT",
"LastTradeTime":1777888799,
"BuyPrice":1469.7,
"LastTradePrice":1469.7,
"SellPrice":1469.8,
"MessageType":"LastQuoteShortResult"}],
"MessageType":"LastQuoteArrayShortResult"}
```

---

## GetLastQuoteArrayShortWithClose

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshortwithclose/ — last updated 2026-05-04T17:08:55 — captured 2026-07-15

### GetLastQuoteArrayShortWithClose : Returns LastTradePrice of multiple Symbols – max 25 in single call (short) with Close of Previous Day

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentIdentifiers** | *Value of instrument identifiers – max 25* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

### What is returned ?

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **Close** (Previous Trading Day's Close), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

### Sample request(JavaScript)

```javascript
            {
                MessageType: "GetLastQuoteArrayShortWithClose",
                Exchange: "NFO",
				isShortIdentifiers: "True",
                InstrumentIdentifiers: [{Value:"NIFTY-I"}, {Value:"BANKNIFTY-I"}, {Value:"RELIANCE-I"}]
            };
     doSend(request);
```

### Example of returned data in JSON format

```json
{"Result":[{
"Exchange":"NFO",
"InstrumentIdentifier":"NIFTY-I",
"LastTradeTime":1777888801,
"BuyPrice":24190.0,
"Close":24098.2,
"LastTradePrice":24199.0,
"SellPrice":24200.0,
"MessageType":"LastQuoteShortWithCloseResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"BANKNIFTY-I",
"LastTradeTime":1777888799,
"BuyPrice":55100.0,
"Close":55195.2,
"LastTradePrice":55100.0,
"SellPrice":55130.0,
"MessageType":"LastQuoteShortWithCloseResult"},
{"Exchange":"NFO",
"InstrumentIdentifier":"RELIANCE-I",
"LastTradeTime":1777888799,
"BuyPrice":1469.7,
"Close":1435.2,
"LastTradePrice":1469.7,
"SellPrice":1469.8,
"MessageType":"LastQuoteShortWithCloseResult"}],
"MessageType":"LastQuoteArrayShortWithCloseResult"}
```

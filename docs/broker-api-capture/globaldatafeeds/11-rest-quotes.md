# GlobalDataFeeds REST API — Last Quote Functions

Single-symbol and multi-symbol (array) last-quote functions of the GlobalDataFeeds REST API: GetLastQuote, GetLastQuoteShort, GetLastQuoteShortWithClose, GetLastQuoteArray, GetLastQuoteArrayShort, GetLastQuoteArrayShortWithClose.

Sources:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquote/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshort/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshortwithclose/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearray/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshort/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshortwithclose/

---

## GetLastQuote

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquote/ — last updated 2024-07-02T18:07:56 — captured 2026-07-15

### GetLastQuote : Returns LastTradePrice of Single Symbol (detailed)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent. |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuote/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=XXX_MCXSCHANADLH_25Jun2015_XX_X&xml=true&isShortIdentifier=true
```

(link target in original page: `http://endpoint:port/GetLastQuote/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=351245&xml=true&isShortIdentifier=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close)

LastTradeTime, ServerTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
{ "AVERAGETRADEDPRICE": 871.68, "BUYPRICE": 870.1, "BUYQTY": 1, "CLOSE": 870.9, "EXCHANGE": "MCX", "HIGH": 875, "INSTRUMENTIDENTIFIER": "XXX_MCXSCHANADLH_25Jun2015_XX_X", "LASTTRADEPRICE": 875, "LASTTRADEQTY": 2, "LASTTRADETIME": 1391863200, "LOW": 868.1, "OPEN": 871.2, "OPENINTEREST": 221, "PREOPEN": false, "QUOTATIONLOT": 100, "SELLPRICE": 874, "SELLQTY": 1, "SERVERTIME": 1391821719, "TOTALQTYTRADED": 17, "VALUE": 1481850}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<Realtime
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
Exchange="MCX" InstrumentIdentifier="XXX_MCXSCHANADLH_25Jun2015_XX_X" LastTradeTime="2-8-2014 12:40:00 PM" ServerTime="2-8-2014 1:08:39 AM" AverageTradedPrice="871.68" BuyPrice="870.1" BuyQty="1" Close="870.9" High="875" Low="868.1" LastTradePrice="875" LastTradeQty="2" Open="871.2" OpenInterest="221" QuotationLot="100" SellPrice="874" SellQty="1" TotalQtyTraded="17" Value="1481850" PreOpen="false"
/>
```

CSV:

```csv
AverageTradedPrice,BuyPrice,BuyQty,Close,Exchange,High,InstrumentIdentifier,LastTradePrice,LastTradeQty,LastTradeTime,Low,Open,OpenInterest,PreOpen,QuotationLot,SellPrice,SellQty,ServerTime,TotalQtyTraded,Value,PriceChange,PriceChangePercentage,OpenInterestChange
47488.15,47180.1,30,47660.95,NFO,47953.85,BANKNIFTY-I,47180.1,450,1713434400000,47040,47750,1949265,False,15,47180.15,45,1713434400000,2747610,130478915821.5,-480.85,-1.01,118890
```

---

## GetLastQuoteShort

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshort/ — last updated 2024-07-10T13:40:04 — captured 2026-07-15

### GetLastQuoteShort : Returns LastTradePrice of Single Symbol (short)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuoteShort/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=XXX_MCX0_25MAR2021_XX_X&xml=true&isShortIdentifier=true
```

(link target in original page: `http://endpoint:port/GetLastQuoteShort/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=XXX_MCX0_25Jun2015_XX_X&xml=true&isShortIdentifier=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
{ "BUYPRICE": 870.1, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX0_25MAR2021_XX_X", "LASTTRADEPRICE": 875, "LASTTRADETIME": 1391863200, "SELLPRICE": 874}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<Realtime
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
Exchange="MCX" InstrumentIdentifier="XXX_MCX0_25MAR2021_XX_X" LastTradeTime="2-8-2014 12:40:00 PM" BuyPrice="870.1" LastTradePrice="875" SellPrice="874"
/>
```

CSV:

```csv
BuyPrice,Exchange,InstrumentIdentifier,LastTradePrice,LastTradeTime,SellPrice
24312.2,NFO,NIFTY-I,24313.05,1720598910000,24313.8
```

Also available additional function [**GetLastQuoteShortWithClose**](http://nimblerest.lisuns.com:4531/?name=GetLastQuoteShortWithClose.htm) that returns additional close price of previous day.

---

## GetLastQuoteShortWithClose

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshortwithclose/ — last updated 2024-07-10T13:41:01 — captured 2026-07-15

### GetLastQuoteShortWithClose : Returns LastTradePrice of Single Symbol (short) with Close of Previous Day

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifier** | *Value of instrument identifier* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifier** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuoteShortWithClose/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=XXX_MCX0_25MAR2021_XX_X&xml=true&isShortIdentifier=true
```

(link target in original page: `http://endpoint:port/GetLastQuoteShortWithClose/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifier=XXX_MCX0_25Jun2015_XX_X&xml=true&isShortIdentifier=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **Close** (Previous Trading Day's Close), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
{ "BUYPRICE": 870.1, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX0_25MAR2021_XX_X", "LASTTRADEPRICE": 875, "LASTTRADETIME": 1391863200, "SELLPRICE": 874, "CLOSE": 860.2}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<Realtime
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
Exchange="MCX" InstrumentIdentifier="XXX_MCX0_25MAR2021_XX_X" LastTradeTime="2-8-2014 12:40:00 PM" BuyPrice="870.1" LastTradePrice="875" SellPrice="874" Close="860.2"
/>
```

CSV:

```csv
BuyPrice,Exchange,InstrumentIdentifier,LastTradePrice,LastTradeTime,SellPrice,Close
24308.35,NFO,NIFTY-I,24308.45,1720599014000,24309.35,24485.65
```

---

## GetLastQuoteArray

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearray/ — last updated 2024-07-02T18:07:59 — captured 2026-07-15

### GetLastQuoteArray : Returns LastTradePrice of multiple Symbols – max 25 in single call (detailed)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuoteArray/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25MAR2021_XX_X+XXX_MCX1_25Jun2015_XX_X+XXX_MCX2_25Jun2015_XX_X&xml=true&isShortIdentifiers=true
```

(link target in original page: `http://endpoint:port/GetLastQuoteArray/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25Jun2015_XX_X+XXX_MCX1_25Jun2015_XX_X+XXX_MCX2_25Jun2015_XX_X&xml=true&isShortIdentifiers=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, ServerTime, AverageTradedPrice** (VWAP), **Close** (previous Day's Close), **Open, High, Low, LastTradePrice, LastTradeQty, TotalQtyTraded, BuyPrice** (Bid), **BuyQty** (Bid Size), **SellPrice** (Ask), **SellQty** (Sell Size), **OpenInterest, QuotationLot** (Lot Size), **Value** (Turnover), **PreOpen** (if PreOpen), **PriceChange** (Change in Price compared to previous trading day's Close), **PriceChangePercentage** (Percentage Change in Price compared to previous trading day's Close), **OpenInterestChange** (Change in Open Interest compared to previous trading day's Close)

LastTradeTime, ServerTime : In JSON Response, these values are expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
[
{ "AVERAGETRADEDPRICE": 123.45, "BUYPRICE": 123.45, "BUYQTY": 1, "CLOSE": 123.45, "EXCHANGE": "MCX", "HIGH": 123.45, "INSTRUMENTIDENTIFIER": "XXX_MCX0_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADEQTY": 2, "LASTTRADETIME": 1415114727418, "LOW": 123.45, "OPEN": 123.45, "OPENINTEREST": 10, "PREOPEN": false, "QUOTATIONLOT": 1, "SELLPRICE": 123.45, "SELLQTY": 1, "SERVERTIME": 1415114727418, "TOTALQTYTRADED": 10, "VALUE": 1230.45},{ "AVERAGETRADEDPRICE": 123.45, "BUYPRICE": 123.45, "BUYQTY": 1, "CLOSE": 123.45, "EXCHANGE": "MCX", "HIGH": 123.45, "INSTRUMENTIDENTIFIER": "XXX_MCX1_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADEQTY": 2, "LASTTRADETIME": 1415114727418, "LOW": 123.45, "OPEN": 123.45, "OPENINTEREST": 10, "PREOPEN": false, "QUOTATIONLOT": 1, "SELLPRICE": 123.45, "SELLQTY": 1, "SERVERTIME": 1415114727418, "TOTALQTYTRADED": 10, "VALUE": 1230.45},

{ "AVERAGETRADEDPRICE": 123.45, "BUYPRICE": 123.45, "BUYQTY": 1, "CLOSE": 123.45, "EXCHANGE": "MCX", "HIGH": 123.45, "INSTRUMENTIDENTIFIER": "XXX_MCX2_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADEQTY": 2, "LASTTRADETIME": 1415114727418, "LOW": 123.45, "OPEN": 123.45, "OPENINTEREST": 10, "PREOPEN": false, "QUOTATIONLOT": 1, "SELLPRICE": 123.45, "SELLQTY": 1, "SERVERTIME": 1415114727418, "TOTALQTYTRADED": 10, "VALUE": 1230.45}
]
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<RealtimeArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX0_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" ServerTime="11-4-2014 5:20:31 PM" AverageTradedPrice="123.45" BuyPrice="123.45" BuyQty="1" Close="123.45" High="123.45" Low="123.45" LastTradePrice="123.45" LastTradeQty="2" Open="123.45" OpenInterest="10" QuotationLot="1" SellPrice="123.45" SellQty="1" TotalQtyTraded="10" Value="1230.45" PreOpen="false" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX1_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" ServerTime="11-4-2014 5:20:31 PM" AverageTradedPrice="123.45" BuyPrice="123.45" BuyQty="1" Close="123.45" High="123.45" Low="123.45" LastTradePrice="123.45" LastTradeQty="2" Open="123.45" OpenInterest="10" QuotationLot="1" SellPrice="123.45" SellQty="1" TotalQtyTraded="10" Value="1230.45" PreOpen="false" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX2_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" ServerTime="11-4-2014 5:20:31 PM" AverageTradedPrice="123.45" BuyPrice="123.45" BuyQty="1" Close="123.45" High="123.45" Low="123.45" LastTradePrice="123.45" LastTradeQty="2" Open="123.45" OpenInterest="10" QuotationLot="1" SellPrice="123.45" SellQty="1" TotalQtyTraded="10" Value="1230.45" PreOpen="false" />
</RealtimeArray>
```

CSV:

```csv
AverageTradedPrice,BuyPrice,BuyQty,Close,Exchange,High,InstrumentIdentifier,LastTradePrice,LastTradeQty,LastTradeTime,Low,Open,OpenInterest,PreOpen,QuotationLot,SellPrice,SellQty,ServerTime,TotalQtyTraded,Value,PriceChange,PriceChangePercentage,OpenInterestChange
22377.82,22381.95,100,22358.2,NFO,22448,NIFTY-I,22382,1800,1713851904000,22345,22448,9980700,False,50,22384.6,100,1713851905000,1786550,39979094321,23.8,0.11,341450
```

---

## GetLastQuoteArrayShort

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshort/ — last updated 2024-07-10T15:13:06 — captured 2026-07-15

### GetLastQuoteArrayShort : Returns LastTradePrice of multiple Symbols – max 25 in single call (short)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuoteArrayShort/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25MAR2021_XX_X+XXX_MCX1_25MAR2021_XX_X+XXX_MCX2_25MAR2021_XX_X&xml=true&isShortIdentifiers=true
```

(link target in original page: `http://endpoint:port/GetLastQuoteArrayShort/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25Jun2015_XX_X+XXX_MCX1_25Jun2015_XX_X+XXX_MCX2_25Jun2015_XX_X&xml=true&isShortIdentifiers=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
[
{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX0_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45},{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX1_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45},

{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX2_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45}
]
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<RealtimeArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX0_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX1_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX2_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" />
</RealtimeArray>
```

CSV:

```csv
BuyPrice,Exchange,InstrumentIdentifier,LastTradePrice,LastTradeTime,SellPrice
24359.1,NFO,NIFTY-I,24360.7,1720604525000,24360.7
52325.85,NFO,BANKNIFTY-I,52327.95,1720604526000,52329.35
23648.75,NFO,FINNIFTY-I,23648.7,1720604525000,23654.6
```

Also available additional function [**GetLastQuoteArrayShortWithClose**](http://nimblerest.lisuns.com:4531/?name=GetLastQuoteArrayShortWithClose.htm) that returns additional close price of previous day.

---

## GetLastQuoteArrayShortWithClose

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshortwithclose/ — last updated 2024-07-10T15:11:49 — captured 2026-07-15

### GetLastQuoteArrayShortWithClose : Returns LastTradePrice of multiple Symbols – max 25 in single call (short) with Close of Previous Day

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentIdentifiers** | *Values of instrument identifiers, max. limit is 25 instruments per single request* | How to get list of available instruments and identifiers you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/). You will need to use URL Econding for instrument identifiers with special characters (like BAJAJ-AUTO, M&M, L&T, NIFTY 50, etc.) as explained [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **isShortIdentifiers** | *[true]/[false], default = [false]* | Optional parameter. By default function will use long instrument identifier format. Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |

**Example**

```http
http://endpoint:port/GetLastQuoteArrayShortWithClose/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25MAR2021_XX_X+XXX_MCX1_25MAR2021_XX_X+XXX_MCX2_25MAR2021_XX_X&xml=true&isShortIdentifiers=true
```

(link target in original page: `http://endpoint:port/GetLastQuoteArrayShortWithClose/?accessKey=0a0b0c&exchange=MCX&instrumentIdentifiers=XXX_MCX0_25Jun2015_XX_X+XXX_MCX1_25Jun2015_XX_X+XXX_MCX2_25Jun2015_XX_X&xml=true&isShortIdentifiers=false`)

**What is returned ?**

**Exchange, InstrumentIdentifier** (Symbol), **LastTradeTime, BuyPrice** (Bid Price), **Close** (Previous Trading Day's Close), **LastTradePrice, SellPrice** (Ask Price)

LastTradeTime : In JSON Response, this value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [https://www.epochconverter.com/](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page)

**Example of returned data**

JSON:

```json
[
{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX0_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45, "CLOSE": 123.45},

{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX1_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45, "CLOSE": 123.45},

{ "BUYPRICE": 123.45, "EXCHANGE": "MCX", "INSTRUMENTIDENTIFIER": "XXX_MCX2_25MAR2021_XX_X", "LASTTRADEPRICE": 123.45, "LASTTRADETIME": 1415114727418, "SELLPRICE": 123.45, "CLOSE": 123.45}
]
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<RealtimeArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX0_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" Close="123.45" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX1_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" Close="123.45" />
<Value Exchange="MCX" InstrumentIdentifier="XXX_MCX2_25MAR2021_XX_X" LastTradeTime="11-4-2014 5:20:31 PM" BuyPrice="123.45" LastTradePrice="123.45" SellPrice="123.45" Close="123.45" />
</RealtimeArray>
```

CSV:

```csv
BuyPrice,Exchange,InstrumentIdentifier,LastTradePrice,LastTradeTime,SellPrice,Close
24359.65,NFO,NIFTY-I,24360,1720604484000,24360,24485.65
52320.8,NFO,BANKNIFTY-I,52325,1720604483000,52325,52615.75
23648.7,NFO,FINNIFTY-I,23648.7,1720604483000,23654,23715.5
```

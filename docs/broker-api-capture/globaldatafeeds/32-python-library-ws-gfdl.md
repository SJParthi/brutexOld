# GlobalDataFeeds — Official Python Library (ws-gfdl / gfdlws)

Scope: Full capture of the official GFDL Python WebSocket API library page on PyPI — package metadata, complete project description (README), function reference, usage examples, release history, and distribution files.

Source URLs:
- https://pypi.org/project/ws-gfdl/

---

## ws-gfdl 1.1.0 (PyPI)

> Source: https://pypi.org/project/ws-gfdl/ — captured 2026-07-15

**Package:** `ws-gfdl` 1.1.0

```bash
pip install ws-gfdl
```

- **Released:** Aug 12, 2022 (latest release)
- **Summary:** Beta Package to implement WebSocket API of Global Datafeeds
- **Maintainer:** developer.gfdl (https://pypi.org/user/developer.gfdl/)
- **License:** MIT License
- **Author:** Global Datafeeds (developer@globaldatafeeds.in)
- **Tags:** python, first package
- **Requires:** Python >=3.9
- **Classifiers:**
  - License :: OSI Approved :: MIT License
  - Operating System :: OS Independent
  - Programming Language :: Python :: 3

Note: although the PyPI project is named `ws-gfdl`, the project description below (the package README) instructs installing/importing the library as `gfdlws`.

---

## Project description

# Global Datafeeds Python Library Description

## Reference document for implementing python library of WebSocket API by Global Datafeeds

WebSocket API by Global Datafeeds is Versatile, most Modern, simple yet powerful API. This API will provide on demand real time and historical data in JSON format from server. It is Suitable for Web, Mobile as well as Desktop Applications. Below are the list of function and their respective details available in WebSocket API. For more detail Documentation & Support refer to the page by **Global Datafeeds** here: https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/documentation/websockets-api-documentation/

This python library is developed to save time and efforts of clients while working on implementation of WebSocket API of Global Datafeeds. List of functions (Data Requests) available is given below.

## List of Functions:

1. SubscribeRealtime
2. SubscribeSnapshot
3. GetLastQuote
4. GetLastQuoteShort
5. GetLastQuoteShortWithClose
6. GetLastQuoteArray
7. GetLastQuoteArrayShort
8. GetLastQuoteArrayShortWithClose
9. GetSnapshot
10. GetHistory
11. GetHistoryAfterMarket
12. GetExchanges
13. GetInstrumentsOnSearch
14. GetInstruments
15. GetInstrumentTypes
16. GetProducts
17. GetExpiryDates
18. GetOptionTypes
19. GetStrikePrices
20. GetServerInfo
21. GetLimitation
22. GetMarketMessages
23. GetExchangeMessages
24. GetLastQuoteOptionChain
25. GetExchangeSnapshot
26. SubscribeRealtimeGreeks
27. GetLastQuoteOptionGreeks
28. GetLastQuoteArrayOptionGreeks
29. GetLastQuoteOptionGreeksChain
30. Parameters

---

# Getting Started

## Installing a Library

Client need to install this library to install the library by issuing the command from python console

```bash
python3 -m pip install gfdlws
```

or on windows command prompt

```bash
pip install gfdlws
```

---

## How to Connect using WebSocket API

Once installation is completed client can connect to API server by the code sample given below:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
```

Here in above code sample:

- Endpoint : endpoint is URL like 'ws://endpoint:port'. End point URL and Port number will be provided by Global Datafeeds.
- API Key : This is the key which will be authenticated and once key was authenticated, client can request data by using function given in the above list.

**Response**
If key is authenticated server will send below response in JOSN format

```json
{"Complete":true,"Message":"Welcome!","MessageType":"AuthenticateResult"}"
```

---

# Realtime data requests

Client can get real-time data through API. This data will be of two types:

1. Client can subscribe Realtime data and get the data for every second.
2. Client can subscribe Realtime snapshots data, returns data snapshot as per Periodicity & Period values.

---

## SubscribeRealtime

In this client will receive the data with 1 second frequency. This data will start once request is sent and will continue till market is open. Below is the sample code to get this data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.realtime.get(con,<Exchange>,<InstrumentIdentifier>, <Unsubscribe Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.realtime.get(con,'NSE','SBIN','false')
```

Client will get data for 'SBIN' in response for each second. Sample response is given below. This response will be in JSON format.

**Response**

```json
{"Exchange":"NSE","InstrumentIdentifier":"SBIN","LastTradeTime":1658127120,"ServerTime":1658127121,"AverageTradedPrice":484.46,"BuyPrice":487.15,"BuyQty":167,"Close":479.1,"High":487.45,"Low":482.1,"LastTradePrice":487.2,"LastTradeQty":111,"Open":484.55,"OpenInterest":0,"QuotationLot":1.0,"SellPrice":487.2,"SellQty":200,"TotalQtyTraded":4141512,"Value":2006396903.52,"PreOpen":false,"PriceChange":8.1,"PriceChangePercentage":1.69,"OpenInterestChange":0,"MessageType":"RealtimeResult"}
```

---

## SubscribeSnapshot

In this client will receive real-time Snapshots data, this function returns snapshot data as per Periodicity & Period values provided. This snapshot data will start once request is sent and will continue till market is open. Below is the sample code to get snapshot data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.realsnapshot.get(con,<Exchange>,<InstrumentIdentifier>,<Periodicity>,<Period>,<Unsubscribe Optional[true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys
con = gw.ws.connect(<EndPoint>, <API Key>)
gw.realsnapshot.get(con,'MCX','CRUDEOIL-I','MINUTE','1','false')
```

Client will get data for 'CRUDEOIL' current contact in response for each minute when minute id finished. 'CRUDEOIL-I' is InstrumentIdentifier (Symbol) to understand more about the 'Symbol Naming Conventions' check: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/symbol-naming-conventions/

Sample response is given below. This response will be in JSON format.

**Response**

```json
{"Exchange":"MCX","InstrumentIdentifier":"CRUDEOIL-I","Periodicity":"MINUTE","Period":1,"LastTradeTime":1658139420,"TradedQty":46,"OpenInterest":1494,"Open":7935.0,"High":7939.0,"Low":7935.0,"Close":7939.0,"MessageType":"RealtimeSnapshotResult"}
```

---

## GetLastQuote

In this client will receive record of last (latest one) 'LastTradePrice' of single symbol with more details like Open, High, Low Close and many more fields. This function will return single latest record of the requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquote.get(con,<Exchange>,<InstrumentIdentifier>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquote.get(con,'NFO','NIFTY-I','false')
```

Client will get latest data for 'NIFTY' current contact in response. 'NIFTY-I' is InstrumentIdentifier (Symbol).

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"ServerTime":1658138400,"AverageTradedPrice":16234.07,"BuyPrice":16310.0,"BuyQty":4900,"Close":16068.3,"High":16520.0,"Low":16170.45,"LastTradePrice":16312.95,"LastTradeQty":800,"Open":16199.0,"OpenInterest":11179850,"QuotationLot":50.0,"SellPrice":16312.95,"SellQty":300,"TotalQtyTraded":8102000,"Value":131528435140.0,"PreOpen":false,"PriceChange":244.65,"PriceChangePercentage":1.52,"OpenInterestChange":-767300,"MessageType":"LastQuoteResult"}
```

---

## GetLastQuoteShort

In this client will receive record of last (latest one) 'LastTradePrice' of single symbol in short with limited fields/values. This function will return single latest record of the requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteshort.get(con,<Exchange>,<InstrumentIdentifier >,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteshort.get(con,'NFO','NIFTY-I','false')
```

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"BuyPrice":16310.0,"LastTradePrice":16312.95,"SellPrice":16312.95,"MessageType":"LastQuoteShortResult"}
```

---

## GetLastQuoteShortWithClose

In this client will receive record of last (latest one) 'LastTradePrice' of single symbol in short with Close of Previous Day. This function will return single latest record of the requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteshortclose.get(con,<Exchange>,<InstrumentIdentifier>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteshortclose.get(con,'NFO','NIFTY-I','false')
```

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"BuyPrice":16310.0,"Close":16068.3,"LastTradePrice":16312.95,"SellPrice":16312.95,"MessageType":"LastQuoteShortWithCloseResult"}
```

---

## GetLastQuoteArray

In this client will receive record of last (latest one) 'LastTradePrice' of multiple symbols with more details like Open, High, Low Close and many more fields. In this single call client can request maximum 25 number of symbols. This function will return array of latest record single for each requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearray.get(con,<Exchange>,<InstrumentIdentifiers>,<isShortIdentifiers Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearray.get(con,'NFO','[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]','false')
```

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"ServerTime":1658138400,"AverageTradedPrice":16234.07,"BuyPrice":16310.0,"BuyQty":4900,"Close":16068.3,"High":16520.0,"Low":16170.45,"LastTradePrice":16312.95,"LastTradeQty":800,"Open":16199.0,"OpenInterest":11179850,"QuotationLot":50.0,"SellPrice":16312.95,"SellQty":300,"TotalQtyTraded":8102000,"Value":131528435140.0,"PreOpen":false,"PriceChange":244.65,"PriceChangePercentage":1.52,"OpenInterestChange":-767300,"MessageType":"LastQuoteResult"},
{"Exchange":"NFO","InstrumentIdentifier":"BANKNIFTY-I","LastTradeTime":1658138401,"ServerTime":1658138401,"AverageTradedPrice":35164.12,"BuyPrice":35424.25,"BuyQty":25,"Close":34782.3,"High":35690.0,"Low":34887.1,"LastTradePrice":35435.0,"LastTradeQty":125,"Open":35690.0,"OpenInterest":2348500,"QuotationLot":25.0,"SellPrice":35435.0,"SellQty":550,"TotalQtyTraded":3293900,"Value":115827094868.0,"PreOpen":false,"PriceChange":652.7,"PriceChangePercentage":1.88,"OpenInterestChange":253425,"MessageType":"LastQuoteResult"}],"MessageType":"LastQuoteArrayResult"}
```

---

## GetLastQuoteArrayShort

In this client will receive array of records of lastest 'LastTradePrice' of multiple symbols in short with limited fields/values. In this single call client can request maximum 25 number of symbols. This function will return array of latest record single for each requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys
con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearrayshort.get(con,<Exchange>,<InstrumentIdentifiers>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys
con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearrayshort.get(con,'NFO','[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]','false')
```

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"BuyPrice":16310.0,"LastTradePrice":16312.95,"SellPrice":16312.95,"MessageType":"LastQuoteShortResult"},{"Exchange":"NFO","InstrumentIdentifier":"BANKNIFTY-I","LastTradeTime":1658138401,"BuyPrice":35424.25,"LastTradePrice":35435.0,"SellPrice":35435.0,"MessageType":"LastQuoteShortResult"}],"MessageType":"LastQuoteArrayShortResult"}
```

---

## GetLastQuoteArrayShortWithClose

In this client will receive array of records of lastest 'LastTradePrice' of multiple symbols in short with limited fields/values with Close of Previous Day. In this single call client can request maximum 25 number of symbols. This function will return array of latest record single for each requested symbol. Below is the sample code to get last quote data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearrayshortclose.get(con,<Exchange>,<InstrumentIdentifiers>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquotearrayshortclose.get(con,'NFO','[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]','false')
```

**Response**

```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"BuyPrice":16310.0,"Close":16068.3,"LastTradePrice":16312.95,"SellPrice":16312.95,"MessageType":"LastQuoteShortWithCloseResult"},
{"Exchange":"NFO","InstrumentIdentifier":"BANKNIFTY-I"," LastTradeTime":1658138401,"BuyPrice":35424.25,"Close":34782.3,"LastTradePrice":35435.0,"SellPrice":35435.0,"MessageType":"LastQuoteShortWithCloseResult"}],"MessageType":"LastQuoteArrayShortWithCloseResult"}
```

---

## GetSnapshot

In this client will receive snapshots data. This function returns latest snapshot data as per Periodicity & Period values provided. In this single call client can request maximum 25 number of symbols. This function will return array of latest single record for each requested symbol. Below is the sample code to get snapshot data:

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.snapshot.get(con,<Exchange>,<InstrumentIdentifiers>,<Periodicity>,<Period>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.snapshot.get(con,'NFO','[{"Value":"BANKNIFTY-I"},{"Value":"NIFTY-I"}]','MINUTE','1','false')
```

**Response**

```json
{"InstrumentIdentifier":"FUTIDX_BANKNIFTY_28JUL2022_XX_0","Exchange":"NFO","LastTradeTime":1658204580,"TradedQty":31200,"OpenInterest":2306425,"Open":35436.5,"High":35443.0,"Low":35420.15,"Close":35434.0,"TokenNumber":null},
{"InstrumentIdentifier":"FUTIDX_NIFTY_28JUL2022_XX_0","Exchange":"NFO","LastTradeTime":1658204580,"TradedQty":25550,"OpenInterest":11313250,"Open":16288.45,"High":16291.75,"Low":16286.5,"Close":16287.4,"TokenNumber":null}],"MessageType":"SnapshotResult"}
```

---

# Historical data requests

## GetHistory

This will returns historical data as per the periodicity and Period provided in request. Returned dat will be Tick, Minute candle or EOD. Client will get 2 types of response depend on the request

- **Getbyperiod:** This request will return the data between the provided period. Client need to provide From time and To time in this call.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.history.getbyperiod(con,<Exchange>,<InstrumentIdentifier>,<Periodicity>, <Period>,<From>,<To>,<UserTag>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.history.getbyperiod(con,'NFO','NIFTY-I','MINUTE','1','1658115000','1658138400','dhananjay','false')
```

Here in this request From and To is a numerical value of UNIX Timestamp like '1658138400' (18-07-2022 15:30:00). This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit Epoch Converter (https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa.

As a response to above call client will get all the 1 minute candle records between provided period.

- **Getcaldle:** This request will return the data in number of latest candles. Client need to provide number of candles in this call.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.history.getbyperiod(con,<Exchange>,<InstrumentIdentifier>,<Periodicity>, <Period>,<Max>,<UserTag>,<isShortIdentifier Optional [true]/[false][default=false]>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.history.getcaldle(con,'NFO','NIFTY-I','MINUTE','1','10','dhananjay','false')
```

As a response to above call, client will get all 10 number of 1 minute candle records.

Sample response for both the calls is given below. This response will be in JSON format.

**Response**

*OHLC Format:*

```json
{"LastTradeTime":1658138400,"QuotationLot":50,"TradedQty":800,"OpenInterest":11179850,"Open":16312.95,"High":16312.95,"Low":16312.95,"Close":16312.95},
{"LastTradeTime":1658138340,"QuotationLot":50,"TradedQty":89800,"OpenInterest":11179850,"Open":16305.0,"High":16312.0,"Low":16304.45,"Close":16310.0}
```

*TICK Format:*

```json
[{"LastTradeTime":1594186301,"LastTradePrice":10775.0,"QuotationLot":75,"TradedQty":225,"OpenInterest":12176625,"BuyPrice":10774.3,"BuyQty":75,"SellPrice":10775.0,"SellQty":3150},
{"LastTradeTime":1594186300,"LastTradePrice":10774.0,"QuotationLot":75,"TradedQty":0,"OpenInterest":12176625,"BuyPrice":10774.3,"BuyQty":75,"SellPrice":10775.0,"SellQty":3375}
```

---

## GetHistoryAfterMarket

This function returns historical data in the form of Tick, Minute or EOD as per request till previous working day. This function is useful for the users / service providers who want to provide services like back-testing as they do not need live / current day's data. This should also save their API costs. To receive current day's historical data via this function, you will need to send request after market is closed. Requests for this function are same as **History.**

---

## GetExchanges

Client will get the list of available exchanges configured for API Key.

**Syntax / Example:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.exchanges.get(con))
```

**Response**

```json
{"Value":"CDS"},{"Value":"MCX"},{"Value":"NFO"},{"Value":"NSE"},{"Value":"NSE_IDX"}],"MessageType":"ExchangesResult"}
```

---

## GetInstrumentsOnSearch

This function will returns array of max. 20 instruments by selected exchange and 'search string'.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.instonsearch.get(con,<Exchnage>,<Search>,<InstrumentType Optional>,<Product Optional>,<Expiry Optional>,<OptionType Optional>,<StrikePrice Optional>,<OnlyActive Optional>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.instonsearch.get(con,'NFO','NIFTY')
```

**Response**

```json
{"Identifier":"OPTIDX_NIFTY_21JUL2022_CE_12900","Name":"OPTIDX","Expiry":"21Jul2022","StrikePrice":12900.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"CE","ProductMonth":"21Jul2022","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY21JUL2212900CE","QuotationLot":50.0,"Description":"","TokenNumber":"69695","LowPriceRange":2667.2,"HighPriceRange":4221.1},
{"Identifier":"OPTIDX_NIFTY_21JUL2022_PE_14350","Name":"OPTIDX","Expiry":"21Jul2022","StrikePrice":14350.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"PE","ProductMonth":"21Jul2022","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY21JUL2214350PE","QuotationLot":50.0,"Description":"","TokenNumber":"53857","LowPriceRange":0.05,"HighPriceRange":3.6}
```

---

## GetInstruments

This function will returns array of instruments by selected exchange.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.instruments.get(con,<Exchnage>,<InstrumentType Optional>,<Product Optional>,<Expiry Optional>,<OptionType Optional>,<StrikePrice Optional>,<OnlyActive Optional>)
```

**Example**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.instruments.get(con, 'NFO')
```

**Response**

```json
{"Identifier":"OPTSTK_EXIDEIND_29SEP2022_PE_116","Name":"OPTSTK","Expiry":"29Sep2022","StrikePrice":116.0,"Product":"EXIDEIND","PriceQuotationUnit":"","OptionType":"PE","ProductMonth":"29Sep2022","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"EXIDEIND29SEP22116PE","QuotationLot":3600.0,"Description":"","TokenNumber":"100889","LowPriceRange":0.05,"HighPriceRange":3.25},
{"Identifier":"OPTSTK_EXIDEIND_29SEP2022_PE_115","Name":"OPTSTK","Expiry":"29Sep2022","StrikePrice":115.0,"Product":"EXIDEIND","PriceQuotationUnit":"","OptionType":"PE","ProductMonth":"29Sep2022","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"EXIDEIND29SEP22115PE","QuotationLot":3600.0,"Description":"","TokenNumber":"100887","LowPriceRange":0.05,"HighPriceRange":3.2},
```

---

## GetInstrumentTypes

This function will returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.) under provided exchange.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.instrumenttypes.get(con, <Exchange>)
```

**Example:**

```python
gw.instrumenttypes.get(con, 'NFO')
```

**Response**

```json
{"Value":"FUTIDX"},{"Value":"FUTSTK"},{"Value":"OPTIDX"},{"Value":"OPTSTK"}
```

---

## GetProducts

This function will returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL etc.) under provided exchange.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.products.get(con, <Exchange>,<InstrumentType Optional>)
```

**Example**

```python
gw.products.get(con, 'NFO')
```

**Response**

```json
{"Value":"AARTIIND"},{"Value":"ABB"},{"Value":"ABBOTINDIA"},{"Value":"ABCAPITAL"},{"Value":"ABFRL"}
```

---

## GetExpiryDates

This function will returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.) as per the parameter provided.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.expirydates.get(con, <Exchange>,<InstrumentType Optional>,<Product Optional>)
```

**Example**

```python
gw.expirydates.get(con, 'NFO')
```

**Response**

```json
{"Value":"21JUL2022"},{"Value":"26JUL2022"},{"Value":"28JUL2022"},{"Value":"02AUG2022"}
```

---

## GetOptionTypes

This function will returns list of Option Types (e.g. CE, PE, etc.) as per the parameter provided.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.optiontypes.get(con, <Exchnage>,<InstrumentType Optional>,<Product Optional>,<Expiry Optional>)
```

**Example**

```python
gw.optiontypes.get(con, 'NFO')
```

**Response**

```json
{"Value":"CE"},{"Value":"PE"}
```

---

## GetStrikePrices

This function will returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.) as per the provided parameter.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.strikeprices.get(con, <Exchange>,<InstrumentType Optional>,<Product Optional>,<Expiry Optional>,<OptionType Optional>)
```

**Example**

```python
gw.strikeprices.get(con, 'NFO')
```

**Response**

```json
{"Value":"3000"},{"Value":"3010"},{"Value":"3020"},{"Value":"3040"},{"Value":"3050"},{"Value":"3060"},{"Value":"3080"},{"Value":"3100"}
```

---

## GetServerInfo

This function will returns information about server where connection is made.

**Syntax / Example:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.serverinfo.get(con)
```

**Response**

```json
{"ServerID":"444A-C7","MessageType":"ServerInfoResult"}
```

---

## GetLimitation

This function will returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.)

**Syntax / Example:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.limitation.get(con)
```

**Response**

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
("AllowedInstruments":-1,"DataDelay":0,"ExchangeName":"NSE_IDX"}
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

(Note: the JSON above is reproduced verbatim from the PyPI page, including its typographical defects such as `},<` and `(` in place of `{`.)

---

## GetMarketMessages

This function will returns array of last market messages related to selected exchange.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.marketmessages.get(con,<Exchange>)
```

**Example**

```python
gw.marketmessages.get(con,'NFO')
```

**Response**

```json
{"Request":{"Exchange":"NFO","MessageType":"GetMarketMessages"},"Result":[{"ServerTime":1658288700,"SessionID":0,"MarketType":"Normal Market Open","Exchange":"NFO","MessageType":"MarketMessagesItemResult"}],"MessageType":"MarketMessagesResult"}
```

---

## GetExchangeMessages

This function will returns array of last exchange messages related to selected exchange.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.marketmessages.get(con,<Exchnage>)
```

**Example**

```python
gw.marketmessages.get(con,'NFO')
```

**Response**

```json
{"ServerTime":1391822398,"Identifier":"Market","Message":"Members are requested to note that ...","MessageType":"ExchangeMessagesItemResult"},
{"ServerTime":1391822399,"Identifier":"Market","Message":"2013 shall be levied subsequently.","MessageType":"ExchangeMessagesItemResult"}
```

---

## GetLastQuoteOptionChain

This function will returns LastTradePrice of entire OptionChain of requested underlying.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteoptionchain.get(con,<Exchange>,<Product Optional>,<Expiry Optional>,<OptionType Optional>,<StrikePrice Optional>)
```

**Example**

```python
gw.lastquoteoptionchain.get(con,'NFO','NIFTY')
```

**Response**
As a response to above call, client will get LastTradePrice of entire OptionChain. Sample response in JSON format can be downloaded from: https://globaldatafeeds.in/resources/GetLastQuoteOptionChainResponse_JSON.zip

---

## GetExchangeSnapshot

This function will return entire Exchange Snapshot as per Period & Periodicity.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.exchangesnapshot.get(con,<Exchange>,<Periodicity>,<Period>,<InstrumentType Optional>,<From Optional>,<To Optional>,<nonTraded Optional>)
```

**Example**

```python
gw.exchangesnapshot.get(con,'NFO','MINUTE','1')
```

**Response**
As a response to above call, client will get entire Exchange Snapshot as per Period & Periodicity. Sample response of 1 minute ExchangeSnapshot in JSON format can be downloaded from: https://globaldatafeeds.in/resources/GetExchangeSnapshot_1Min_JSON.zip

---

## SubscribeRealtimeGreeks

This function will return Realtime Greek values of single Token, returns market data with 1 second frequency. This data will start once request is sent and will continue till market is open.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.realtimegreeks.get(con,<Exchange>,<Token>)
```

**Example**

```python
gw.realtimegreeks.get(con,'NFO','70222')
```

**Response**

```json
{"Exchange":"NFO","Token":"70222","Timestamp":1660292501,"IV":0.11267127841711044,"Delta":0.2549774944782257,"Theta":-6.706413269042969,"Vega":7.45473575592041,"Gamma":0.0012275002663955092,"IVVwap":0.1129087582230568,"Vanna":2.9635884761810303,"Charm":-0.026660971343517303,"Speed":3.0227099614421604E-06,"Zomma":-0.006058624479919672,"Color":-5.450446406030096E-05,"Volga":4533.580078125,"Veta":132.6668243408203,"ThetaGammaRatio":-5463.4716796875,"ThetaVegaRatio":-0.8996178507804871,"DTR":-0.038019951432943344,"MessageType":"SubscribeRealtimeGreeks"}
```

Token number can be get by using **GetInstruments** or **GetInstrumentsOnSearch** request.

---

## GetLastQuoteOptionGreeks

This function will return Last Traded Option Greek values of Single Symbol.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteoptiongreeks.get(con,<Exchange>,<Token>)
```

**Example**

```python
gw.lastquoteoptiongreeks.get(con,'NFO','70222')
```

**Response**

```json
{"Exchange":"NFO","Token":"70222","Timestamp":1660293046,"IV":0.11312421411275864,"Delta":0.2495148926973343,"Theta":-6.668005466461182,"Vega":7.353806018829346,"Gamma":0.0012113795382902026,"IVVwap":0.11292107403278352,"Vanna":3.061586618423462,"Charm":-0.027760693803429604,"Speed":3.05659523291979E-06,"Zomma":-0.005707615520805 1205,"Color":-5.1753351726802066E-05,"Volga":4794.48974609375,"Veta":136.56570434570312,"ThetaGammaRatio":-5504.47265625,"ThetaVegaRatio":-0.9067420959472656,"DTR":-0.037419721484184265,"MessageType":"LastQuoteOptionGreeksResult"}
```

---

## GetLastQuoteArrayOptionGreeks

This function will returns Last Traded Option Greek values of multiple Symbols – max 25 in single call.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteoptiongreeks.get(con,<Exchange>,<Token>)
```

**Example**

```python
gw.lastquoteoptiongreeks.get(con,'NFO','70222')
```

**Response**

```json
[{"Exchange":"NFO","Token":"70222","Timestamp":1660293432,"IV":0.11281614750623704,"Delta":0.24168457090854645,"Theta":-6.5438151359558105,"Vega":7.216636657714844,"Gamma":0.0011960952542722223,"IVVwap":0.11291974782943726,"Vanna":3.2351670265197754,"Charm":-0.02933545969426632,"Speed":3.1457541354029672E-06,"Zomma":-0.005284379702061415,"Color":-4.791706669493577E-05,"Volga":5243.822265625,"Veta":142.34922790527344,"ThetaGammaRatio":-5470.9814453125,"ThetaVegaRatio":-0.906767964363098,"DTR":-0.036933280527591705,"MessageType":"LastQuoteOptionGreeksResult"},
{"Exchange":"NFO","Token":"70222","Timestamp":1660293046,"IV":0.11312421411275864,"Delta":0.2495148926973343,"Theta":-6.668005466461182,"Vega":7.353806018829346,"Gamma":0.0012113795382902026,"IVVwap":0.11292107403278352,"Vanna":3.061586618423462,"Charm":-0.027760693803429604,"Speed":3.05659523291979E-06,"Zomma":-0.0057076155208051205,"Color":-5.1753351726802066E-05,"Volga":4794.48974609375,"Veta":136.56570434570312,"ThetaGammaRatio":-5504.47265625,"ThetaVegaRatio":-0.9067420959472656,"DTR":-0.037419721484184265}],"MessageType":"LastQuoteArrayOptionGreeksResult"}
```

Token number can be get by using **GetInstruments** or **GetInstrumentsOnSearch** request.

---

## GetLastQuoteOptionGreeksChain

This function will returns Returns Last Traded Option Greek values of entire OptionChain of requested underlying.

**Syntax:**

```python
import gfdlws as gw
import sys

con = gw.ws.connect(<EndPoint>, <API Key>)
gw.lastquoteoptiongreekschain.get(con,<Exchange>,<Product>)
```

**Example**

```python
gw.lastquoteoptiongreekschain.get(con,'NFO','NIFTY')
```

**Response**
As a response to above call, client will get Last Traded Option Greek values of entire OptionChain. Sample response in JSON format can be downloaded from: https://globaldatafeeds.in/resources/OptionGreeksChainWithQuoteResponse_JSON.zip

---

# Parameter List

| Parameter | Description |
| --- | --- |
| **Exchange** | Name of supported exchange. |
| **InstrumentIdentifier** | Name of the symbol |
| **InstrumentIdentifiers** | List of symbols names maximum 25 symbols can be added in list. |
| **Unsubscribe** | Buy default subscribes to Realtime data. If [true], instrumentIdentifier is unsubscribed |
| **Periodicity** | String value of required periodicity. *["TICK"/"MINUTE"/"HOUR"/"DAY"/"WEEK"/"MONTH", default = "TICK"]* |
| **Period** | Period for historical data. Can be applied for [MINUTE]/[HOUR]/[DAY]. Periodicity types *[Numerical value 1, 2, 3…, default = 1]* |
| **From** | It means starting timestamp for called historical data. |
| **To** | It means ending timestamp for request. |
| **isShortIdentifier** / **isShortIdentifiers** | Functions will use short instrument identifier format if set as [true]. Example of ShortIdentifiers are NIFTY25MAR21FUT, RELIANCE25MAR21FUT, NIFTY25MAR2115000CE, etc. |
| **MAX** | It is the limit returned data records. |
| **Product** | Name of supported Product. To get the list of product refer to the above **Product** function. |
| **Expiry** | Expiry dates for the exchange. To get the list of expiries refer to the above **Expiry** function. |
| **OptionType** | Expiry dates for the exchange. To get the list of OptionType refer to the above **OptionType** function. |
| **StrikePrice** | Expiry dates for the exchange. To get the list of StrikePrice refer to the above **StrikePrice** function. |
| **Token** / **Tokens** | Token numbers of instruments. To get the token refer to the above **GetInstruments** or **GetInstrumentsOnSearch** functions. |
| **userTag** | It will be string which returns with response. |
| **onlyActive** | By default, function will return only active instruments. [true]/[false], default = [true] |
| **nonTraded** | When true, results are sent with data of even non traded instruments. When false, data of only traded instruments during that period is sent. Optional, default value is "false" |

---

## Release history

| Version | Released |
| --- | --- |
| 1.1.0 (this version) | Aug 12, 2022 |
| 1.0.6 | Aug 12, 2022 |
| 1.0.5 | Aug 12, 2022 |
| 1.0.4 | Aug 12, 2022 |
| 1.0.3 | Aug 12, 2022 |
| 1.0.2 | Aug 12, 2022 |
| 1.0.1 | Aug 11, 2022 |
| 1.0.0 | Aug 11, 2022 |

## Download files

### Source Distribution

- `ws-gfdl-1.1.0.tar.gz` (31.5 kB, Source), uploaded Aug 12, 2022 via twine/4.0.1 CPython/3.9.0
  - URL: https://files.pythonhosted.org/packages/b7/54/70fea85f2efaf3b8e2ea35b64113c04e66217a5d1f8cb0acbff71c14c1c0/ws-gfdl-1.1.0.tar.gz
  - SHA256: `e3cfe0081cd1718f148bb79470ddace4a49fba02262b7dbd49bf126055dd3e6f`
  - MD5: `4c46262d6692d8020a2e251153f598fe`
  - BLAKE2b-256: `b75470fea85f2efaf3b8e2ea35b64113c04e66217a5d1f8cb0acbff71c14c1c0`

### Built Distribution

- `ws_gfdl-1.1.0-py3-none-any.whl` (31.1 kB, Python 3 / py3-none-any), uploaded Aug 12, 2022 via twine/4.0.1 CPython/3.9.0
  - URL: https://files.pythonhosted.org/packages/f3/e2/265e4b1fe93f8a06ad7b091d0b1910881db0166da06797b3b05130918e6c/ws_gfdl-1.1.0-py3-none-any.whl
  - SHA256: `34d38e20fbb9190bc833fa00e473172c700be4158940e4b8fd8d3eddd8c92c67`
  - MD5: `d1997e130244d4864887b7327e7fe19c`
  - BLAKE2b-256: `f3e2265e4b1fe93f8a06ad7b091d0b1910881db0166da06797b3b05130918e6c`

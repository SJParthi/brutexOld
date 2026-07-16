# GlobalDataFeeds REST API — Instruments & Metadata Functions

Instrument master and metadata lookup functions of the GlobalDataFeeds REST API: GetExchanges, GetInstrumentsOnSearch, GetInstruments, GetInstrumentTypes, GetProducts, GetExpiryDates, GetOptionTypes, GetStrikePrices, GetHolidays.

Sources:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstrumentsonsearch/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getholidays-2/

---

## GetExchanges

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/ — last updated 2024-07-03T08:22:53 — captured 2026-07-15

### GetExchanges : Returns array of available exchanges configured for API Key

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetExchanges/?accessKey=0a0b0c&xml=true
```

**What is returned ?**

**Exchange**

**Example of returned data**

JSON:

```json
{"EXCHANGES": ["MCX","NSE"]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<Exchanges
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>MCX</Value>
<Value>NSE</Value>
</Exchanges>
```

CSV:

```csv
Value
CDS
MCX
NFO
NSE
NSE_IDX
```

---

## GetInstrumentsOnSearch

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstrumentsonsearch/ — last updated 2024-07-03T08:23:00 — captured 2026-07-15

### GetInstrumentsOnSearch : Returns array of max. 20 instruments by selected exchange and 'search string'

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **search** | *String value like RELIANCE (in CAPS)* | Search word value (pattern for search) in CAPS |
| **instrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) |
| **optionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) |
| **strikePrice** | *String value like 0* | Optional parameter. Name of supported StrikePrice. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **onlyActive** | *[true]/[false], default = [true]* | Optional parameter. By default, function will return only active instruments. Function will return all (active + expired) instruments if value equals false. |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |

**Example**

```http
http://endpoint:port/GetInstrumentsOnSearch/?accessKey=0a0b0c&exchange=NFO&xml=true&search=NIFTY&detailedInfo=true
```

**What is returned ?**

**Identifier** (Symbol), **Name** (Instrument Type), **Expiry** (Expiry Date), **StrikePrice, Product, OptionType, ProductMonth, TradeSymbol** (ShortIdentifier for same Identifier/Symbol), **QuotationLot** (Lot Size), When detailedInfo=true, following additional information will be sent (if available from Exchange) **TokenNumber** (Token number of Symbol), **LowPriceRange** (Lower circuit limit), **HighPriceRange** (Upper circut limit)

**Example of returned data**

JSON:

```json
{"INSTRUMENTS": [{"Identifier":"OPTIDX_NIFTY_15JUL2021_CE_14650",
"Name":"OPTIDX","Expiry":"15Jul2021","StrikePrice":14650.0,
"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"CE",
"ProductMonth":"15Jul2021","UnderlyingAsset":"","UnderlyingAssetExpiry":"",
"IndexName":"","TradeSymbol":"NIFTY15JUL2114650CE","QuotationLot":75.0,
"Description":"","TokenNumber":"44079","LowPriceRange":26.15,
"HighPriceRange":2104.15},{"Identifier":"OPTIDX_NIFTY_15JUL2021_PE_15700",
"Name":"OPTIDX","Expiry":"15Jul2021","StrikePrice":15700.0,
"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"PE",
"ProductMonth":"15Jul2021","UnderlyingAsset":"",
"UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY15JUL2115700PE",
"QuotationLot":75.0,"Description":"","TokenNumber":"44171",
"LowPriceRange":0.05,"HighPriceRange":600.5},
"MessageType":"InstrumentsOnSearchResult"}
]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<Instruments
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value Exchange="NFO" Identifier="OPTIDX_NIFTY_22JUL2021_PE_16500"
Name="OPTIDX" Expiry="22Jul2021" StrikePrice="16500" Product="NIFTY"
PriceQuotationUnit="" OptionType="PE" ProductMonth="22Jul2021"
UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName=""
TradeSymbol="NIFTY22JUL2116500PE" QuotationLot="75" Description=""
TokenNumber="45880" LowPriceRange="0.05" HighPriceRange="1521.45"/>
<Value Exchange="NFO" Identifier="OPTIDX_NIFTY_22JUL2021_PE_16150"
Name="OPTIDX" Expiry="22Jul2021" StrikePrice="16150" Product="NIFTY"
PriceQuotationUnit="" OptionType="PE" ProductMonth="22Jul2021"
UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName=""
TradeSymbol="NIFTY22JUL2116150PE" QuotationLot="75" Description=""
TokenNumber="47534" LowPriceRange="0.05" HighPriceRange="956.85"/
</Instruments>
```

CSV:

```csv
Exchange,Expiry,Identifier,IndexName,Name,OptionType,PriceQuotationUnit,Product,ProductMonth,StrikePrice,TradeSymbol,UnderlyingAsset,UnderlyingAssetExpiry,QuotationLot,Description,TokenNumber,LowPriceRange,HighPriceRange
NFO,25Apr2024,OPTIDX_NIFTY_25APR2024_PE_23100,,OPTIDX,PE,,NIFTY,25Apr2024,23100.0,NIFTY25APR2423100PE,,,50.0,,68247,251.2,1257.6
```

---

## GetInstruments

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/ — last updated 2025-07-09T11:27:16 — captured 2026-07-15

### GetInstruments : Returns array of instruments by selected exchange

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) (Applicable only for Future and Option) |
| **product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) (Applicable only for Future and Option) |
| **optionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) (Applicable only for Future and Option) |
| **strikePrice** | *String value like 0* | Optional parameter. Name of supported StrikePrice. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/) (Applicable only for Future and Option) |
| **series** | *String value like* EQ | Optional parameter.We can use this parameter to filter those symbols by series wise . (Applicable only for Equity / CASH) |
| **showDummyISIN** | *[true]/[false], default = [false]* | Optional parameter. When true, instruments with dummy ISIN will be included in response. (Applicable only for Equity / CASH) |
| **showETF** | *[true]/[false], default = [false]* | Optional parameter. When true, ETF instruments will be included in response. (Applicable only for Equity / CASH) |
| **showInterOperable** | *[true]/[false], default = [false]* | Optional parameter. When true, Inter Operable instruments will be included in response with special character (#/ $). (Applicable only for Equity / CASH) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |
| **onlyActive** | *[true]/[false], default = [true]* | Optional parameter. By default, function will return only active instruments. Function will return all (active + expired) instruments if value equals false. |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |

**Example**

```http
https://endpoint:port/GetInstruments/?accessKey=a0b0b0&exchange=NSE&detailedInfo=true&series=EQ&ShowDummyISIN=false&ShowETF=false&OnlyActive=true&showInteroperable=false&xml=true
```

**What is returned ?**

**Identifier** (Symbol), **Name** (Instrument Type), **Expiry** (Expiry Date), **StrikePrice, Product, OptionType, ProductMonth, TradeSymbol** (ShortIdentifier for same Identifier/Symbol), **QuotationLot** (Lot Size), When **detailedInfo=true**, following additional information will be sent (if available from Exchange) **TokenNumber** (Token number of Symbol), **LowPriceRange** (Lower circuit limit), **HighPriceRange** (Upper circuit limit), **ISIN code, Series, High52Week, Low52Week.**

**Example of returned data**

JSON:

```json
{"INSTRUMENTS": [{"EXCHANGE": "NFO","EXPIRY": "28Nov2024","IDENTIFIER": "FUTIDX_NIFTY_28NOV2024_XX_0","INDEXNAME": "","NAME": "FUTIDX","OPTIONTYPE": "XX","PRICEQUOTATIONUNIT": "","PRODUCT": "NIFTY","PRODUCTMONTH": "28Nov2024","STRIKEPRICE": 0.0,"TRADESYMBOL": "NIFTY28NOV24FUT","UNDERLYINGASSET": "","UNDERLYINGASSETEXPIRY": "","QUOTATIONLOT": 25.0,"DESCRIPTION": "","TOKENNUMBER": "35089","LOWPRICERANGE": "21241.55","HIGHPRICERANGE": "25961.9","ISIN": "","52WeekHigh": "0","52WeekLow": "0","SERIES": ""},{"EXCHANGE": "NFO","EXPIRY": "26Dec2024","IDENTIFIER": "FUTIDX_NIFTY_26DEC2024_XX_0","INDEXNAME": "","NAME": "FUTIDX","OPTIONTYPE": "XX","PRICEQUOTATIONUNIT": "","PRODUCT": "NIFTY","PRODUCTMONTH": "26Dec2024","STRIKEPRICE": 0.0,"TRADESYMBOL": "NIFTY26DEC24FUT","UNDERLYINGASSET": "","UNDERLYINGASSETEXPIRY": "","QUOTATIONLOT": 25.0,"DESCRIPTION": "","TOKENNUMBER": "35005","LOWPRICERANGE": "21370.7","HIGHPRICERANGE": "26119.75","ISIN": "","52WeekHigh": "0","52WeekLow": "0","SERIES": ""},{"EXCHANGE": "NFO","EXPIRY": "30Jan2025","IDENTIFIER": "FUTIDX_NIFTY_30JAN2025_XX_0","INDEXNAME": "","NAME": "FUTIDX","OPTIONTYPE": "XX","PRICEQUOTATIONUNIT": "","PRODUCT": "NIFTY","PRODUCTMONTH": "30Jan2025","STRIKEPRICE": 0.0,"TRADESYMBOL": "NIFTY30JAN25FUT","UNDERLYINGASSET": "","UNDERLYINGASSETEXPIRY": "","QUOTATIONLOT": 25.0,"DESCRIPTION": "","TOKENNUMBER": "35006","LOWPRICERANGE": "21516.95","HIGHPRICERANGE": "26298.5","ISIN": "","52WeekHigh": "0","52WeekLow": "0","SERIES": ""}]}
```

XML:

```xml
<Instruments xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
<Value Exchange="NFO" Identifier="FUTIDX_NIFTY_28NOV2024_XX_0" Name="FUTIDX" Expiry="28Nov2024" StrikePrice="0" Product="NIFTY" PriceQuotationUnit="" OptionType="XX" ProductMonth="28Nov2024" UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName="" TradeSymbol="NIFTY28NOV24FUT" QuotationLot="25" Description="" TokenNumber="35089" LowPriceRange="21241.55" HighPriceRange="25961.9" IsActive="true" ISIN="" Series="" High52Week="0" Low52Week="0"/>
<Value Exchange="NFO" Identifier="FUTIDX_NIFTY_26DEC2024_XX_0" Name="FUTIDX" Expiry="26Dec2024" StrikePrice="0" Product="NIFTY" PriceQuotationUnit="" OptionType="XX" ProductMonth="26Dec2024" UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName="" TradeSymbol="NIFTY26DEC24FUT" QuotationLot="25" Description="" TokenNumber="35005" LowPriceRange="21370.7" HighPriceRange="26119.75" IsActive="true" ISIN="" Series="" High52Week="0" Low52Week="0"/>
<Value Exchange="NFO" Identifier="FUTIDX_NIFTY_30JAN2025_XX_0" Name="FUTIDX" Expiry="30Jan2025" StrikePrice="0" Product="NIFTY" PriceQuotationUnit="" OptionType="XX" ProductMonth="30Jan2025" UnderlyingAsset="" UnderlyingAssetExpiry="" IndexName="" TradeSymbol="NIFTY30JAN25FUT" QuotationLot="25" Description="" TokenNumber="35006" LowPriceRange="21516.95" HighPriceRange="26298.5" IsActive="true" ISIN="" Series="" High52Week="0" Low52Week="0"/>

</Instruments>
```

CSV:

```csv
Exchange,Expiry,Identifier,IndexName,Name,OptionType,PriceQuotationUnit,Product,ProductMonth,StrikePrice,TradeSymbol,UnderlyingAsset,UnderlyingAssetExpiry,QuotationLot,Description,TokenNumber,LowPriceRange,HighPriceRange,ISIN,52WeekHigh,52WeekLow,Series
NFO,28Nov2024,FUTIDX_NIFTY_28NOV2024_XX_0,,FUTIDX,XX,,NIFTY,28Nov2024,0.0,NIFTY28NOV24FUT,,,25.0,,35089,21241.55,25961.9,,0,0,
NFO,26Dec2024,FUTIDX_NIFTY_26DEC2024_XX_0,,FUTIDX,XX,,NIFTY,26Dec2024,0.0,NIFTY26DEC24FUT,,,25.0,,35005,21370.7,26119.75,,0,0,
NFO,30Jan2025,FUTIDX_NIFTY_30JAN2025_XX_0,,FUTIDX,XX,,NIFTY,30Jan2025,0.0,NIFTY30JAN25FUT,,,25.0,,35006,21516.95,26298.5,,0,0,
```

---

## GetInstrumentTypes

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/ — last updated 2024-07-10T15:08:36 — captured 2026-07-15

### GetInstrumentTypes : Returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetInstrumentTypes/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**InstrumentTypes**

**Example of returned data**

JSON:

```json
{"INSTRUMENTTYPES": ["FUTIDX","FUTSTK","OPTIDX","OPTSTK"]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<StringArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>FUTIDX</Value>
<Value>FUTSTK</Value>
<Value>OPTIDX</Value>
<Value>OPTSTK</Value>
</StringArray>
```

CSV:

```csv
Value
FUTIDX
FUTSTK
OPTIDX
OPTSTK
```

---

## GetProducts

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/ — last updated 2024-07-03T08:23:14 — captured 2026-07-15

### GetProducts : Returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetProducts?accessKey=0a0b0c&exchange=MCX&xml=true
```

(link target in original page: `http://endpoint:port/GetProducts/?accessKey=0a0b0c&exchange=MCX&xml=true`)

**What is returned ?**

**Products**

**Example of returned data**

JSON:

```json
{"PRODUCTS": ["BANKNIFTY","NIFTY","GAIL","GLENMARK",]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<StringArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>BANKNIFTY</Value>
<Value>NIFTY</Value>
<Value>GAIL</Value>
<Value>GLENMARK</Value>
</StringArray>
```

CSV:

```csv
Value
BANKNIFTY NIFTY GAIL GLENMARK
```

---

## GetExpiryDates

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/ — last updated 2024-07-03T08:23:21 — captured 2026-07-15

### GetExpiryDates : Returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetExpiryDates/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**ExpiryDates**

**Example of returned data**

JSON:

```json
{"EXPIRYDATES": ["27Aug2015","30Jul2015","17Jul2015","24Sep2015","31Dec2015"]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<StringArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>27Aug2015</Value>
<Value>30Jul2015</Value>
<Value>17Jul2015</Value>
</StringArray>
```

CSV:

```csv
Value
25APR2024
02MAY2024
09MAY2024
16MAY2024
23MAY2024
30MAY2024
27JUN2024
26SEP2024
26DEC2024
```

---

## GetOptionTypes

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/ — last updated 2024-07-03T08:23:39 — captured 2026-07-15

### GetOptionTypes : Returns list of Option Types (e.g. CE, PE, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetOptionTypes/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**OptionTypes**

**Example of returned data**

JSON:

```json
{"OPTIONTYPES": ["XX","CE","PE"]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<StringArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>XX</Value>
<Value>CE</Value>
<Value>PE</Value>
</StringArray>
```

CSV:

```csv
Value
FF XX CE PE
```

---

## GetStrikePrices

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/ — last updated 2024-12-09T17:19:58 — captured 2026-07-15

### GetStrikePrices : Returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.)

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) |
| **instrumentType** | *String value like OPTSTK* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) |
| **product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) |
| **expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) |
| **optionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |
| **format** | *CSV* | Optional parameter. When format=CSV, data in CSV format will be returned. Please make sure not to pass xml parameter (neither True nor False) when format=CSV is sent |

**Example**

```http
http://endpoint:port/GetStrikePrices/?accessKey=0a0b0c&exchange=MCX&xml=true
```

**What is returned ?**

**StrikePrices**

**Example of returned data**

JSON:

```json
{"INSTRUMENTTYPES": ["11000","10000","8500","8600"]}
```

XML:

```xml
<?xml version="1.0" encoding="utf-16" ?>
<StringArray
xmlns:xsd="http://www.w3.org/2001/XMLSchema"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Value>11000</Value>
<Value>10000</Value>
<Value>8500</Value>
<Value>8600</Value>
</StringArray>
```

JSON (labelled "JSON" in the original page; values list):

```csv
Value
11000
10000
8500
8600
```

---

## GetHolidays

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getholidays-2/ — last updated 2026-05-04T14:36:26 — captured 2026-07-15

### GetHolidays : Returns an array of holidays related to the selected exchange.

**Supported parameters**

| Parameter | Value | Description |
|---|---|---|
| **accessKey** | *Access key according to your subscription* | Required parameter. |
| **exchange** | *String value like MCX* | Optional parameter . Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **Year** | *Numerical value like 2025* | Mandatory parameter. Specific year for data retrieval. Example: 2024 |
| **xml** | *[true]/[false], default = [false]* | Optional parameter. By default function will return JSON data. Functions will return XML data if set as [true] |

**Example**

```http
http://endpoint:port/GetHolidays/?accessKey=0a0b0c&exchange=MCX&year=2025&xml=true
```

(link target in original page: `http://endpoint:port/GetMarketMessages/?accessKey=0a0b0c&exchange=MCX&xml=true`)

**What is returned ?**

**Holidays received from the Exchanges**

**Example of returned data in JSON format**

JSON:

```json
{ "Value":[ {"Year":2025,"Date":"01-26-2025","Description":"Republic Day","Exchanges":"MCX;NSE;NSE_IDX;NFO;CDS;BSE;BSE_IDX;BSE_DEBT;BFO;NCX"},
]}
```

XML:

```xml
<Holiday>
<Value>
<HolidayItem Year="2025" Date="01-26-2025" Description="Republic Day" Exchanges="MCX;NSE;NSE_IDX;NFO;CDS;BSE;BSE_IDX;BSE_DEBT;BFO;NCX"/>
</Value>
</Holiday>
```

CSV:

```csv
Year,Date,Description,Exchanges
2025,"01-26-2025",Republic Day,MCX;NSE;NSE_IDX;NFO;CDS;BSE;BSE_IDX;BSE_DEBT;BFO;NCX
```

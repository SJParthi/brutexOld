# GlobalDataFeeds WebSockets API — Instruments & Metadata Functions

Instrument discovery and metadata functions of the GlobalDataFeeds WebSockets API: GetExchanges, GetInstrumentsOnSearch, GetInstruments, GetInstrumentTypes, GetProducts, GetExpiryDates, GetOptionTypes, GetStrikePrices and GetHolidays.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getinstrumentsonsearch-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getholidays/

---

## GetExchanges

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/ — last updated 2024-07-10T12:40:01 — captured 2026-07-15

### GetExchanges : Returns array of available exchanges configured for API Key

### What is returned ?

**Exchange**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetExchanges",
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"Result":[
{"Value":"MCX"},
{"Value":"NFO"},
{"Value":"NSE"},
],
"MessageType":"ExchangesResult"
}
```

---

## GetInstrumentsOnSearch

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getinstrumentsonsearch-2/ — last updated 2025-02-07T16:05:43 — captured 2026-07-15

### GetInstrumentsOnSearch : Returns array of max. 20 instruments by selected exchange and 'search string'

### Supported parameters

| | | |
|---|---|---|
| **Search** | *String value like NIFTY in CAPS* | Search word value (pattern for search) in CAPS |
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **Product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |
| **StrikePrice** | *String value like 0* | Optional parameter. Name of supported StrikePrice. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) |
| **series** | *String value like* EQ | Optional parameter.We can use this parameter to filter those symbols by series wise . |
| **OnlyActive** | *[true]/[false], default = [true]* | Optional parameter. By default, function will return only active instruments. Function will return all (active + expired) instruments if value equals false |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |

### What is returned ?

**Identifier** (Symbol), **Name** (Instrument Type), **Expiry** (Expiry Date), **StrikePrice, Product, OptionType, ProductMonth, TradeSymbol** (ShortIdentifier for same Identifier/Symbol), **QuotationLot** (Lot Size), When detailedInfo=true, following additional information will be sent (if available from Exchange) :TokenNumber, LowPriceRange, HighPriceRange,ISIN,High52Week,Low52Week

### Sample request(JavaScript)

```javascript
{
                MessageType: "GetInstrumentsOnSearch",
                Exchange: "NFO",
		InstrumentType:"FUTIDX",
		Search:"NIFTY",
		//Product:"NIFTY",
		//OptionType:"PE",
		//Expiry:"30jul2020",
                //Series:"EQ",
		detailedInfo: "true"		
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{"Request":{"Exchange":"NFO","Search":"NIFTY","InstrumentType":"FUTIDX","OnlyActive":true,"MessageType":"GetInstrumentsOnSearch"},
"Result":[
{"Identifier":"FUTIDX_NIFTY_29JUL2021_XX_0","Name":"FUTIDX","Expiry":"29Jul2021","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"29Jul2021","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY29JUL21FUT","QuotationLot":50.0,"Description":"","TokenNumber":"53181","LowPriceRange":14250.45,"HighPriceRange":17417.2},
{"Identifier":"FUTIDX_NIFTY_26AUG2021_XX_0","Name":"FUTIDX","Expiry":"26Aug2021","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"26Aug2021","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY26AUG21FUT","QuotationLot":50.0,"Description":"","TokenNumber":"49939","LowPriceRange":14282.8,"HighPriceRange":17456.75},
{"Identifier":"FUTIDX_NIFTY_30SEP2021_XX_0","Name":"FUTIDX","Expiry":"30Sep2021","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"30Sep2021","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY30SEP21FUT","QuotationLot":50.0,"Description":"","TokenNumber":"48740","LowPriceRange":14330.45,"HighPriceRange":17515.0}],
"MessageType":"InstrumentsOnSearchResult"}
```

---

## GetInstruments

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/ — last updated 2025-07-09T12:48:55 — captured 2026-07-15

### GetInstruments : Returns array of instruments by selected exchange

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like FUTIDX* | Optional parameter applicable for F&O segments . Name of supported Instrument Type.<br>How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/)<br>(Applicable only for Future and Option) |
| **Product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/)<br>(Applicable only for Future and Option) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/)<br>(Applicable only for Future and Option) |
| **StrikePrice** | *String value like 0* | Optional parameter. Name of supported StrikePrice. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/)<br>(Applicable only for Future and Option) |
| **Series** | *String value like EQ* | Optional parameter applicable for Stock Segments .We can use this parameter to filter those symbols by series wise (Applicable only for Equity / CASH) |
| **showDummyISIN** | *[true]/[false], default = [false]* | Optional parameter. When true, instruments with<br>dummy ISIN will be included in response.<br>(Applicable only for Equity / CASH) |
| **showETF** | *[true]/[false], default = [false]* | Optional parameter. When true, ETF instruments will<br>be included in response.<br>(Applicable only for Equity / CASH) |
| **showInterOperable** | *[true]/[false], default = [false]* | Optional parameter. When true, Inter Operable instruments will<br>be included in response with special character (#/ $).<br>(Applicable only for Equity / CASH) |
| **OnlyActive** | *[true]/[false], default = [true]* | Optional parameter. By default, function will return only active instruments. Function will return all (active + expired) instruments if value equals false |
| **detailedInfo** | *[true]/[false], default = [false]* | Optional parameter. By default function will return limited fields in response, function will return additional fields in response when this parameter is set as true. |

### What is returned ?

**Identifier** (Symbol), **Name** (Instrument Type), **Expiry** (Expiry Date), **StrikePrice, Product, OptionType, ProductMonth, TradeSymbol** (ShortIdentifier for same Identifier/Symbol), **QuotationLot** (Lot Size), When detailedInfo=true, following additional information will be sent (if available from Exchange)**TokenNumber** (Token number of Symbol), **LowPriceRange** (Lower circuit limit),**HighPriceRange**(Upper circuit limit),**ISIN code, Series, High52Week, Low52Week.**

### Sample request(JavaScript)

```javascript
var request =
{
                MessageType: "GetInstruments",
                Exchange: "NFO",
		InstrumentType:"FUTIDX",
		//Product:"NIFTY",
		//OptionType:"PE",
		//Expiry:"30jul2020",
	        //Series:"EQ",
		//detailedInfo: "true"	
};
var message = JSON.stringify(request);
websocket.send(message); 
```

### Example of returned data in JSON format

```json
{"Request":{"Exchange":"NFO","InstrumentType":"FUTIDX","OnlyActive":true,"MessageType":"GetInstruments"},
"Result":[{"Identifier":"FUTIDX_MIDCPNIFTY_25NOV2024_XX_0","Name":"FUTIDX","Expiry":"25Nov2024","StrikePrice":0.0,"Product":"MIDCPNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"25Nov2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"MIDCPNIFTY25NOV24FUT","QuotationLot":50.0,"Description":"","TokenNumber":"36540","LowPriceRange":10904.75,"HighPriceRange":13328.0,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_FINNIFTY_26NOV2024_XX_0","Name":"FUTIDX","Expiry":"26Nov2024","StrikePrice":0.0,"Product":"FINNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"26Nov2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"FINNIFTY26NOV24FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35117","LowPriceRange":20927.7,"HighPriceRange":25578.25,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_BANKNIFTY_27NOV2024_XX_0","Name":"FUTIDX","Expiry":"27Nov2024","StrikePrice":0.0,"Product":"BANKNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"27Nov2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"BANKNIFTY27NOV24FUT","QuotationLot":15.0,"Description":"","TokenNumber":"35013","LowPriceRange":45267.3,"HighPriceRange":55326.65,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTY_28NOV2024_XX_0","Name":"FUTIDX","Expiry":"28Nov2024","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"28Nov2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY28NOV24FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35089","LowPriceRange":21241.55,"HighPriceRange":25961.9,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTYNXT50_29NOV2024_XX_0","Name":"FUTIDX","Expiry":"29Nov2024","StrikePrice":0.0,"Product":"NIFTYNXT50","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"29Nov2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTYNXT5029NOV24FUT","QuotationLot":10.0,"Description":"","TokenNumber":"35150","LowPriceRange":60980.5,"HighPriceRange":74531.75,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_BANKNIFTY_24DEC2024_XX_0","Name":"FUTIDX","Expiry":"24Dec2024","StrikePrice":0.0,"Product":"BANKNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"24Dec2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"BANKNIFTY24DEC24FUT","QuotationLot":15.0,"Description":"","TokenNumber":"35025","LowPriceRange":45547.55,"HighPriceRange":55669.2,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTY_26DEC2024_XX_0","Name":"FUTIDX","Expiry":"26Dec2024","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"26Dec2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY26DEC24FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35005","LowPriceRange":21370.7,"HighPriceRange":26119.75,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTYNXT50_27DEC2024_XX_0","Name":"FUTIDX","Expiry":"27Dec2024","StrikePrice":0.0,"Product":"NIFTYNXT50","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"27Dec2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTYNXT5027DEC24FUT","QuotationLot":10.0,"Description":"","TokenNumber":"35000","LowPriceRange":61249.75,"HighPriceRange":74860.8,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_MIDCPNIFTY_30DEC2024_XX_0","Name":"FUTIDX","Expiry":"30Dec2024","StrikePrice":0.0,"Product":"MIDCPNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"30Dec2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"MIDCPNIFTY30DEC24FUT","QuotationLot":50.0,"Description":"","TokenNumber":"35004","LowPriceRange":10950.35,"HighPriceRange":13383.8,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_FINNIFTY_31DEC2024_XX_0","Name":"FUTIDX","Expiry":"31Dec2024","StrikePrice":0.0,"Product":"FINNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"31Dec2024","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"FINNIFTY31DEC24FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35426","LowPriceRange":21052.75,"HighPriceRange":25731.1,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_MIDCPNIFTY_27JAN2025_XX_0","Name":"FUTIDX","Expiry":"27Jan2025","StrikePrice":0.0,"Product":"MIDCPNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"27Jan2025","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"MIDCPNIFTY27JAN25FUT","QuotationLot":50.0,"Description":"","TokenNumber":"35007","LowPriceRange":10989.0,"HighPriceRange":13430.95,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_FINNIFTY_28JAN2025_XX_0","Name":"FUTIDX","Expiry":"28Jan2025","StrikePrice":0.0,"Product":"FINNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"28Jan2025","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"FINNIFTY28JAN25FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35239","LowPriceRange":21169.55,"HighPriceRange":25873.9,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_BANKNIFTY_29JAN2025_XX_0","Name":"FUTIDX","Expiry":"29Jan2025","StrikePrice":0.0,"Product":"BANKNIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"29Jan2025","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"BANKNIFTY29JAN25FUT","QuotationLot":15.0,"Description":"","TokenNumber":"35012","LowPriceRange":45852.8,"HighPriceRange":56042.35,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTY_30JAN2025_XX_0","Name":"FUTIDX","Expiry":"30Jan2025","StrikePrice":0.0,"Product":"NIFTY","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"30Jan2025","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTY30JAN25FUT","QuotationLot":25.0,"Description":"","TokenNumber":"35006","LowPriceRange":21516.95,"HighPriceRange":26298.5,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0},

{"Identifier":"FUTIDX_NIFTYNXT50_31JAN2025_XX_0","Name":"FUTIDX","Expiry":"31Jan2025","StrikePrice":0.0,"Product":"NIFTYNXT50","PriceQuotationUnit":"","OptionType":"XX","ProductMonth":"31Jan2025","UnderlyingAsset":"","UnderlyingAssetExpiry":"","IndexName":"","TradeSymbol":"NIFTYNXT5031JAN25FUT","QuotationLot":10.0,"Description":"","TokenNumber":"35279","LowPriceRange":61549.25,"HighPriceRange":75226.9,"ISIN":"","Series":"","High52Week":0.0,"Low52Week":0.0}
],"MessageType":"InstrumentsResult"}
```

---

## GetInstrumentTypes

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/ — last updated 2024-07-10T12:39:25 — captured 2026-07-15

### GetInstrumentTypes : Returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |

### What is returned ?

**InstrumentTypes**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetInstrumentTypes",
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
"MessageType":"GetInstrumentTypes"
},
"Result":[
{"Value":"OPTIDX"},
{"Value":"OPTSTK"}
],
"MessageType":"InstrumentTypesResult"
}
```

---

## GetProducts

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/ — last updated 2024-07-10T12:39:13 — captured 2026-07-15

### GetProducts : Returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL, etc.)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |

### What is returned ?

**Products**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetProducts",
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
"MessageType":"GetProducts"
},
"Result":[
{"Value":"NIFTY"},
{"Value":"GAIL"}
],
"MessageType":"ProductsResult"
}
```

---

## GetExpiryDates

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/ — last updated 2026-05-04T17:21:52 — captured 2026-07-15

### GetExpiryDates : Returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **Product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |

### What is returned ?

**ExpiryDates**

### Example send request(JavaScript)

```javascript
{
MessageType: "GetExpiryDates",
Exchange: "NFO"
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
{
"Request":{
"Exchange":"NFO",
"MessageType":"GetExpiryDates"
},
"Result":[
{"Value":"05MAY2026"},
{"Value":"12MAY2026"},
{"Value":"19MAY2026"},
{"Value":"26MAY2026"}
],
"MessageType":"ExpiryDatesResult"
}
```

---

## GetOptionTypes

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/ — last updated 2024-07-10T12:37:40 — captured 2026-07-15

### GetOptionTypes : Returns list of Option Types (e.g. CE, PE, etc.)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like FUTIDX* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **Product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |

### What is returned ?

**OptionTypes**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetOptionTypes",
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
"MessageType":"GetOptionTypes"
},
"Result":[
{"Value":"PE"},
{"Value":"CE"}
],
"MessageType":"OptionTypesResult"
}
```

---

## GetStrikePrices

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/ — last updated 2024-12-09T15:26:40 — captured 2026-07-15

### GetStrikePrices : Returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.)

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like NFO* | Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **InstrumentType** | *String value like OPTIDX or OPTSTK* | Optional parameter. Name of supported Instrument Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) |
| **Product** | *String value like BANKNIFTY* | Optional parameter. Name of supported Product. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) |
| **Expiry** | *String value like 30Jul2015* | Optional parameter. Name of supported Expiry Date. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) |
| **OptionType** | *String value like CE* | Optional parameter. Name of supported Option Type. How to get list of supported values you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) |

### What is returned ?

**StrikePrices**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetStrikePrices",
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
"MessageType":"GetStrikePrices"
},
"Result":[
{"Value":"11000"},
{"Value":"10000"},
{"Value":"8500"},
{"Value":"8600"}
],
"MessageType":"StrikePricesResult"
}
```

---

## GetHolidays

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getholidays/ — last updated 2026-05-04T14:00:00 — captured 2026-07-15

### GetHolidays : Returns an array of holidays related to the selected exchange.

### Supported parameters

| | | |
|---|---|---|
| **Exchange** | *String value like MCX* | Optional parameter . Name of supported exchange. How to get list of supported exchanges you can find [here](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) |
| **From** | *Numerical value of UNIX Timestamp like '1388534400' (01-01-2014 0:0:0)* | Optional parameter by default it will return the holidays from 2010 .It means starting timestamp for call the holidays. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |
| **To** | *Numerical value of UNIX Timestamp like '1412121600' (10-01-2014 0:0:0)* | Optional parameter by default it will return the holidays from 2010.It means ending timestamp call the holidays. This value is expressed as no. of seconds since Epoch time (i.e. 1st January 1970). Also known as Unix Time. Please Visit [Epoch Converter](https://www.epochconverter.com/) to get formulae to convert human readable time to Epoch and vice versa (scroll to end of their home page) |

### What is returned ?

**Holidays received from the Exchanges**

### Sample request(JavaScript)

```javascript
{
MessageType: "GetHolidays",
From:1735669800,
To:1767119400
};
var message = JSON.stringify(request);
websocket.send(message);
```

### Example of returned data in JSON format

```json
 {
"Request":{
"Exchange":null,
"From":1764527400,
"To":1767033000,
"MessageType":"GetHolidays"
},
"Result":[{"Date":1766601000,"Description":"Christmas",
"Exchanges":"MCX;NSE;NSE_IDX;NFO;CDS;BSE;BSE_IDX;BSE_DEBT;BFO;NCX","MessageType":null}],
"MessageType":"HolidaysResult"}
```

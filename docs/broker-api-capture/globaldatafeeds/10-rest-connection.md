# GlobalDataFeeds REST API — Connection & Client Policies

How to connect to the GlobalDataFeeds REST API (endpoint, port, accessKey), URL-encoding of special characters in instrument identifiers, and the legacy client-access policy files for Silverlight / Adobe Flash applications.

Sources:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/how-to-connect-using-rest-api/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/clientaccesspolicy-xml-for-silverlight-applications/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/crossdomain-policy-for-adobe-flash-applications/

---

## How to Connect using REST API

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/how-to-connect-using-rest-api/ — last updated 2024-07-10T13:19:03 — captured 2026-07-15

### Introduction

- To connect using RESTful APIs, you will need following information :
  - "endpoint" to connect to,
  - "port number" to connect to,
  - "API Key" received from our team to access data

- You will receive this information in the email from our team. Once received, you will need to send request to our server using all of the above parameters **every time when you need data** – as per below syntax :

```http
http://endpoint:port/function_name/?accesskey=apikey
```

- For example, to get list of allowed Exchanges under your key, you can send request as :

```http
http://endpoint:port/GetExchanges/?accesskey=apikey
```

- RESTful API returns data in JSON format by default. If you need response in XML format, you will need to append *xml=true* parameter – as below :

```http
http://endpoint:port/GetExchanges/?accesskey=apikey&xml=true
```

- RESTful API returns data in JSON format by default. If you need response in CSV format, you will need to append *format=CSV* parameter – as below :

```http
http://endpoint:port/GetExchanges/?accesskey=apikey&format=CSV
```

### List of REST API Functions

| REST API Function | Description |
|---|---|
| [GetLastQuote](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquote/) | Returns LastTradePrice of Single Symbol (detailed) |
| [GetLastQuoteShort](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshort/) | Returns LastTradePrice of Single Symbol (short) |
| [GetLastQuoteShortWithClose](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquoteshortwithclose/) | Returns LastTradePrice of Single Symbol (short with Close of Previous Day) |
| [GetLastQuoteArray](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearray/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (detailed) |
| [GetLastQuoteArrayShort](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshort/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (short) |
| [GetLastQuoteArrayShortWithClose](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearrayshortwithclose/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (short with Close of Previous Day) |
| [GetSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getsnapshot/) | Returns latest Snapshot Data of multiple Symbols – max 25 in single call |
| [GetHistory](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/) | Returns historical data (Tick / Minute / EOD) |
| [GetExchanges](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/) | Returns array of available exchanges |
| [GetInstrumentsOnSearch](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstrumentsonsearch/) | Returns array of max. 20 instruments (properties) by selected exchange and 'search word' |
| [GetInstruments](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getinstruments/) | Returns array of instruments (properties) by selected exchange |
| [GetInstrumentTypes](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getinstrumenttypes/) | Returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.) |
| [GetProducts](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getproducts/) | Returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL, etc.) |
| [GetExpiryDates](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexpirydates/) | Returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.) |
| [GetOptionTypes](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getoptiontypes/) | Returns list of Option Types (e.g. CE, PE, etc.) |
| [GetStrikePrices](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getstrikeprices/) | Returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.) |
| [GetServerInfo](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getserverinfo/) | Returns information about server where connection is made |
| [GetLimitation](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlimitation/) | Returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.) |
| [GetMarketMessages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getmarketmessages/) | Returns array of last messages (Market Messages) related to selected exchange |
| [GetExchangeMessages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchangemessages/) | Returns array of last messages (Exchange Messages) related to selected exchange |
| [GetLastQuoteOptionChain](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getlastquoteoptionchain/) | Returns LastTradePrice of entire OptionChain of requested underlying |
| [GetExchangeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/) | Returns entire Exchange Snapshot as per Period & Periodicity |
| [GetLastQuoteOptionGreeks](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreeks-2/) | Returns Last Traded Option Greek values of Single Symbol (detailed) |
| [GetLastQuoteArrayOptionGreeks](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquotearrayoptiongreeks-2/) | Returns Last Traded Option Greek values of multiple Symbols – max 25 in single call (detailed) |
| [GetLastQuoteOptionGreeksChain](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api-global-datafeeds-apis/getlastquoteoptiongreekschain-2/) | Returns Last Traded Option Greek values of entire OptionChain of requested underlying |
|  |  |
| [ClientAccess XML Policy](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/clientaccesspolicy-xml-for-silverlight-applications/) | This is required for Silverlight Applications |
| [CrossDomain Policy](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/crossdomain-policy-for-adobe-flash-applications/) | This is required for Adobe Flash Applications |

### Important Notes

- – API Functions can accept more than 1 parameter. You will need to send additional parameters by appending the query with "&"

  – Our RESTful API accepts and responds to only HTTP GET requests. HTTP POST request or any other type of request WILL NOT work.

  – RESTful API Sends data in JSON format by default. To get data in XML format, append "&xml=true" to your query.

  – Sometimes, API sends [diagnostic messages in plain/text](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/) – instead of actual data requested. For example, when API Key is expired / invalid, etc.. Your application should be able to handle these messages in non-JSON / non-XML format.

---

## Handling Special Characters

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/handling-special-characters/ — last updated 2024-07-02T18:07:57 — captured 2026-07-15

### This is applicable only to users of REST API

When sending requests for InstrumentIdentifiers which contain special characters in their names – for example L&T, M&M, NIFTY 50 (there is a white space between NIFTY and 50), you will need to use URL Encoding as explained [Here](https://www.w3schools.com/tags/ref_urlencode.asp).

Some examples are as below :

| Identifier Name | InstrumentIdentifier to be sent in API Request |
|---|---|
| L&T | L%26T |
| M&M | M%26M |
| NIFTY 50 | NIFTY%2050 |
| NIFTY BANK | NIFTY%20BANK |
| MCDOWELL-N | MCDOWELL%2DN |

---

## ClientAccess Policy for Silverlight Applications

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/clientaccesspolicy-xml-for-silverlight-applications/ — last updated 2024-07-03T08:24:35 — captured 2026-07-15

### ClientAccess XML Policy – for Silverlight Applications

```xml
<access-policy>
<cross-domain-access>
<policy>
<allow-from http-request-headers="SOAPAction">
<domain uri="*"/>
</allow-from>
<grant-to>
<resource path="/" include-subpaths="true"/>
</grant-to>
</policy>
</cross-domain-access>
</access-policy>
```

Check [this article](http://msdn.microsoft.com/en-us/library/cc197955(VS.95).aspx) for more information.

---

## crossdomain Policy for Adobe Flash applications

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/crossdomain-policy-for-adobe-flash-applications/ — last updated 2024-07-03T08:24:44 — captured 2026-07-15

### CrossDomain XML Policy – for Adobe Flash applications

```xml
<cross-domain-policy>
<allow-access-from domain="*"/>
</cross-domain-policy>
```

Please check [this article](http://www.adobe.com/devnet-docs/acrobatetk/tools/AppSec/CrossDomain_PolicyFile_Specification.pdf) for more information.

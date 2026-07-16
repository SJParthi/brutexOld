# GlobalDataFeeds WebSockets API — Connection & Authentication

How to connect to the GlobalDataFeeds WebSockets API and authenticate the session with an API Key.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/how-to-connect-using-websockets-api/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/authenticate/

---

## How To Connect Using WebSockets API

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/how-to-connect-using-websockets-api/ — last updated 2024-07-12T11:09:00 — captured 2026-07-15

### Introduction

To connect using WebSockets API, you will need following information :

– "endpoint" to connect to
– "port number" to connect to
– "API Key" received from our team to access data

The flow of operations should be as follows :

1. Make a connection to the ws://endpoint:port
2. Send Authentication Request using API Key
3. Once authentication is successful, send all other data requests
4. If connection is lost for any reasons, follow same steps from 1 to 3 as above.

### List of WebSockets API Functions

| WebSockets API Functions | Description |
|---|---|
| [Authenticate](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/authenticate/) | Authenticates the user via API Key |
| [SubscribeRealtime](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscriberealtime/) | Subscribe to market data, pushes market data every second (Bid/Ask/Trade) |
| [SubscribeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscribesnapshot/) | Subscribe to market data, pushes market data every minute (and other "Period" values as chosen by user) |
| [GetLastQuote](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquote-2/) | Returns LastTradePrice of Single Symbol (detailed) |
| [GetLastQuoteShort](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshort/) | Returns LastTradePrice of Single Symbol (short) |
| [GetLastQuoteShortWithClose](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquoteshortwithclose/) | Returns LastTradePrice of Single Symbol (short with Close of Previous Day) |
| [GetLastQuoteArray](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquotearray-2/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (detailed) |
| [GetLastQuoteArrayShort](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshort/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (short) |
| [GetLastQuoteArrayShortWithClose](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getlastquotearrayshortwithclose/) | Returns LastTradePrice of multiple Symbols – max 25 in single call (short with Close of Previous Day) |
| [GetSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/) | Returns latest Snapshot Data of multiple Symbols – max 25 in single call |
| [GetHistory](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-gethistory-2/) | Returns historical data (Tick / Minute / EOD) |
| [GetHistoryAfterMarket](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/gethistoryaftermarket-3/) | Returns historical data (Tick / Minute / EOD) till previous working day. |
| [GetExchanges](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchanges-2/) | Returns array of available exchanges |
| [GetInstrumentsOnSearch](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getinstrumentsonsearch-2/) | Returns array of max. 20 instruments by selected exchange and 'search string' |
| [GetInstruments](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/) | Returns array of instruments by selected exchange |
| [GetInstrumentTypes](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstrumenttypes-2/) | Returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.) |
| [GetProducts](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getproducts-2/) | Returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL, etc.) |
| [GetExpiryDates](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexpirydates-2/) | Returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.) |
| [GetOptionTypes](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getoptiontypes-2/) | Returns list of Option Types (e.g. CE, PE, etc.) |
| [GetStrikePrices](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getstrikeprices-2/) | Returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.) |
| [GetServerInfo](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getserverinfo-2/) | Returns information about server where connection is made |
| [GetLimitation](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlimitation-2/) | Returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.) |
| [GetMarketMessages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getmarketmessages-2/) | Returns array of last messages (Market Messages) related to selected exchange |
| [GetExchangeMessages](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getexchangemessages-2/) | Returns array of last messages (Exchange Messages) related to selected exchange |
| [GetLastQuoteOptionChain](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getlastquoteoptionchain/) | Returns LastTradePrice of entire OptionChain of requested underlying |
| [GetExchangeSnapshot](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getexchangesnapshot-2/) | Returns entire Exchange Snapshot as per Period & Periodicity |
| [GetLastQuoteOptionGreeks](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreeks/) | Returns Last Traded Option Greek values of Single Symbol (detailed) |
| [GetLastQuoteArrayOptionGreeks](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquotearrayoptiongreeks/) | Returns Last Traded Option Greek values of multiple Symbols – max 25 in single call (detailed) |
| [GetLastQuoteOptionGreeksChain](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreekschain/) | Returns Last Traded Option Greek values of entire OptionChain of requested underlying |
| [SubscribeRealtimeOptionGreeks](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/subscriberealtimegreeks/) | Subscribe to Realtime Greek values of single Token, returns market data every second (detailed) |

### Important Notes

- – WebSockets API allows only 1 active session with the server with 1 API Key. If you create another session with same key, previous session will be invalidated with response "Access Denied. Key already in use by other session."
- – WebSockets API Sends data only in JSON format
- – Sometimes, API sends [diagnostic messages in plain/text](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/) – instead of actual data requested. For example, when API Key is expired / invalid, etc.. Your application should be able to handle these messages in non-JSON format.

---

## Authenticate

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/authenticate/ — last updated 2024-07-10T12:46:34 — captured 2026-07-15

### Authenticate : Authenticates the user

### What is returned ?

Nothing. This function authenticates the user and sends response accordingly

### Sample Request (JavaScript)

```javascript
 function Authenticate()
  {
     writeToScreen("Authenticate");
    var message = 
    {
       MessageType: "Authenticate",
       Password: accessKey
     };
    doSend(message);
  }
```

### JSON Response

```json
{"Complete":true,"Message":"Welcome!","MessageType":"AuthenticateResult"}"
```

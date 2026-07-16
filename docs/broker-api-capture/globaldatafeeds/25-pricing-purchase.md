# GlobalDataFeeds APIs — Pricing & Purchase

API pricing information, API package comparison matrix, working hours, and eligibility rules on who can purchase the APIs.

Source URLs captured in this file:

- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/api-pricing/
- https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/who-can-purchase/

---

## API Pricing

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/api-pricing/ — last updated 2026-07-11T13:45:41 — captured 2026-07-15

### Introduction

We offer following types of Data through our APIs

**Realtime API**
Realtime data updating at 1 second frequency is available for NSE Stocks, NSE Indices, NSE F&O, NSE Currency ,MCX,BSE Stocks ,BSE Indices & BSE F&O. This is L1 data with single best Bid & Ask details.

**Historical API**
Historical data of Periodicity Tick, Minute, Day, Week, Month is available.

Depending on your requirements, you can go for only realtime, historical or realtime+historical data API

**Realtime OptionChain API**
If you are an OptionChain trader, we also have special API for OptionChain data. Here, you can get realtime price of entire OptionChain of the underlying (say NIFTY, BANKNIFTY, RELIANCE, etc.) in single call.

**Realtime Exchange Snapshot API**
If you do not need realtime data every second but if you need snapshot data of entire Exchange after every few minutes, we also provide API for the same purpose. Currently supported periods are Minute (1,2,5,10,15,30), Hour and Day (end-of-day)

### API Pricing

API is not one-size-fits-all kind of product. We understand that requirements of each user are vastly different from another. Hence we have flexible pricing policy which is tailored to suit your needs. Do contact us with your requirements at :

- Mail us on [sales@globaldatafeeds.in](mailto:sales@globaldatafeeds.in)
- **Working Hours (GMT+5:30)**
  8:30am – 6:00pm (M-F)
  9:30am – 2:30pm (Sat)
- Closed on Sunday and all [Stock Exchange Holidays](http://www.nseindia.com/global/content/market_timings_holidays/market_timings_holidays.htm)
- **Global Financial Datafeeds LLP**
  301, 4th Floor, Krishna Enclave, Jehan Circle, Gangapur Road,
  Nashik – 422005. Maharashtra, India.

### Compare APIs

| API Functions | Realtime + Historical API | Option Chain API | Realtime API | Historical API | Exchange Snapshot API |
| --- | --- | --- | --- | --- | --- |
| **GetExchanges** Returns array of available exchanges configured for API Key | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetInstrumentsOnSearch** Returns array of max. 20 instruments by selected exchange and 'search string' | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetInstruments** Returns array of instruments by selected exchange | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetProducts** Returns list of Products (e.g. NIFTY, BANKNIFTY, GAIL, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetInstrumentTypes** Returns list of Instrument Types (e.g. FUTIDX, FUTSTK, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetExpiryDates** Returns array of Expiry Dates (e.g. 25JUN2020, 30JUL2020, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetOptionTypes** Returns list of Option Types (e.g. CE, PE, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetStrikePrices** Returns list of Strike Prices (e.g. 10000, 11000, 75.5, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetLimitation** Returns user account information (e.g. which functions are allowed, Exchanges allowed, symbol limit, etc.) | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetServerInfo** Returns information about server where connection is made | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetMarketMessages** Returns array of last messages (Market Messages) related to selected exchange | **YES** | **YES** | **YES** | **YES** | **YES** |
| **GetExchangeMessages** Returns array of last messages (Exchange Messages) related to selected exchange | **YES** | **YES** | **YES** | **YES** | **YES** |
| **SubscribeRealtime** Subscribe to market data, pushes market data every second (Bid/Ask/Trade) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **SubscribeSnapshot** Subscribe to market data, pushes market data every minute (and other "Period" values) | **YES** | **YES** | **NO** | **YES** | **NO** |
| **GetLastQuote** Returns LastTradePrice of Single Symbol (detailed) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetLastQuoteShort** Returns LastTradePrice of Single Symbol (short) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetLastQuoteShortWithClose** Returns LastTradePrice of Single Symbol (short with Close of Previous Day) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetLastQuoteArray** Returns LastTradePrice of multiple Symbols – max 25 in single call (detailed) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetLastQuoteArrayShort** Returns LastTradePrice of multiple Symbols – max 25 in single call (short) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetLastQuoteArrayShortWithClose** Returns LastTradePrice of multiple Symbols – max 25 in single call (short with Close of Previous Day) | **YES** | **YES** | **YES** | **NO** | **NO** |
| **GetSnapshot** Returns latest Snapshot Data of multiple Symbols – max 25 in single call | **YES** | **NO** | **NO** | **YES** | **NO** |
| **GetHistory** Returns historical data (Tick / Minute / EOD) | **YES** | **YES** | **NO** | **YES** | **NO** |
| **GetLastQuoteOptionChain** Returns LastTradePrice of entire OptionChain of requested underlying | **NO** | **NO** | **NO** | **NO** | **YES** |
| **GetExchangeSnapshot** Returns entire Exchange Snapshot as per Period & Periodicity | **NO** | **NO** | **NO** | **NO** | **YES** |

### Important Notes

- – WebSockets, DotNet & COM APIs allows only 1 active session with the server with 1 API Key. If you create another session with same key, previous session will be invalidated with response *"Access Denied. Key already in use by other session."*
- – WebSockets API Sends data only in JSON format. RESTful API sends data in JSON & XML format. Data is returned by DotNet and COM API as Objects
- – Sometimes, API sends [diagnostic messages in plain/text](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/documentation-support/diagnostic-api-responses/) – instead of actual data requested. For example, when API Key is expired / invalid, etc.. Your application should be able to handle these messages in non-JSON format.

### Have Questions ?

- For all pre-sales queries, please email us on [sales@globaldatafeeds.in](mailto:sales@globaldatafeeds.in)

### Working Hours for Order Processing & Support

- 8:30am – 6:00pm, M-F (GMT+5:30)
- 9:30am – 2:30pm Sat (GMT+5:30)
- Closed on Sunday and all [Stock Exchange Holidays](http://www.nseindia.com/global/content/market_timings_holidays/market_timings_holidays.htm)

---

## Who can purchase

> Source: https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/who-can-purchase/ — last updated 2024-07-08T23:41:31 — captured 2026-07-15

### Who can purchase our APIs

- **Important :** APIs available here are only for Individual Users for Personal Use. For Commercial Use, you will need to sign agreement with respective Exchanges and pay them the requisite fees. Please see our [Terms and Conditions](https://globaldatafeeds.in/apis/#terms-and-conditions) before placing the order. Please mail us at [sales@globaldatafeeds.in](mailto:sales@globaldatafeeds.in) for more details with your requirements.

  For more information, you can also contact the Stock Exchanges at below links directly with your requirements :
  NSE : [https://www.nseindia.com/market-data/real-time-data-subscription](https://www.nseindia.com/market-data/real-time-data-subscription)
  MCX : [https://www.mcxindia.com/technology/datafeed](https://www.mcxindia.com/technology/datafeed)
  BSE : [https://mock.bseindia.com/market_data_products.html?flag=real](https://mock.bseindia.com/market_data_products.html?flag=real)

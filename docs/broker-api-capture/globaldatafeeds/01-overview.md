# GlobalDataFeeds (GFDL) — Company & API Platform Overview

> Sources:
> - https://globaldatafeeds.in/ (homepage, captured 2026-07-15)
> - https://globaldatafeeds.in/apis/ (Market Data APIs product page, last modified 2025-08-21, captured 2026-07-15)
> - https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/ (API documentation root, captured 2026-07-15)

## Who they are

**Global Financial Datafeeds LLP** ("Global Datafeeds" / GFDL) is an authorized/licensed real-time (L1) market data vendor of Indian stock exchanges. Established **2010** — pioneers in vendor-neutral streaming data dissemination in India.

- **Licensed for:** NSE (Stocks, Indices, F&O & Currency) and MCX — since 2010; BSE (Stocks, Indices, F&O); NCDEX; plus Fundamental Data.
- **Exchange segments served:** NSE (Stocks & Indices), NFO (NSE Futures & Options), NSE CDS (Currency Derivatives), BSE (Stocks & Indices), BFO (BSE Futures & Options), MCX, NCDEX.
- **Scale:** 7,00,00,000+ (7 crore / 70 million) data requests served per day; 60,000+ symbols; 99.995% uptime during market hours via redundant delivery channels with automatic failover.
- **Office:** Global Financial Datafeeds LLP, 301, 4th Floor, Krishna Enclave, Jehan Circle, Gangapur Road, Nashik – 422005, Maharashtra, India.

## Product lines

| Product line | Description | Key link |
|---|---|---|
| Nimble Data Products (desktop/server) | Live data into 25+ charting platforms: NimbleDataPro, NimbleDataPlus (Lite/Pro/VM), NimbleNT (Lite/Pro/VM — NinjaTrader), NimbleExcel (Lite/Pro/VM), NimbleKey (Lite/Pro/VM — MTPredictor, MotiveWave, AbleTrend), NimbleExcelMax (Lite/Pro) | https://globaldatafeeds.in/nimble-data-products/ |
| Market Data APIs | WebSockets, RESTful, DotNet, COM, FIX APIs for custom apps (this documentation set) | https://globaldatafeeds.in/apis/ |
| Fundamental Data APIs | Fundamentals of listed companies (separate doc site) | https://globaldatafeeds.in/fundamental-data-apis/ + https://docs.globaldatafeeds.in/ |
| Market Data Widgets | Embeddable market-data widgets for websites | https://globaldatafeeds.in/market-data-widgets/ |
| Stock Market News APIs | News feed APIs (separate doc site) | https://globaldatafeeds.in/stock-market-news-apis/ + https://newsdocs.globaldatafeeds.in/ |
| After Market / IEOD Data | After-market-hours 1-minute & tick data | https://globaldatafeeds.in/products/#ieod-data |

**Supported charting platforms (25+):** AmiBroker, MetaStock, NinjaTrader, Sierra Chart, Optuma, MS Excel, Updata, MotiveWave, MultiCharts, MTPredictor, AbleTrend, and more — see https://globaldatafeeds.in/supported-platforms-partners-and-integrations/

## Available APIs (Market Data APIs)

Same data & APIs that power GFDL's own retail data products.

### WebSockets APIs
Versatile, most modern, simple yet powerful API. On-demand data in **JSON** format is **pushed from server**. Suitable for Web, Mobile as well as Desktop Applications.
- Authentic, accurate & affordable data with low latency; no commitment required, flexible pricing.
- Supported exchanges: NSE (Stocks & Indices), NFO, NSE CDS, MCX, BSE (Stocks & Indices), BFO.
- OS / Languages: almost any OS & programming language with WebSocket support.
- Available data: real-time, historical, snapshot, delayed, end-of-day, entire exchange, option-chain, option greeks.
- Official Python library: `ws-gfdl` on PyPI — https://pypi.org/project/ws-gfdl/

### RESTful APIs
Most simple API to implement quickly. **HTTP GET** queries receive **JSON/XML** response. On-demand data **pulled from server**. Suitable for Mobile & Web Applications.
- Same exchanges/data coverage as WebSockets.

### DotNet APIs
Most popular API historically. Suitable for any DotNet language (C#, VB.NET & more). On-demand data pushed from server. Suitable for Desktop Applications.

### COM APIs
For all languages which support COM — C++, Java, Python & more. On-demand data pushed from server. Desktop applications. Available data: real-time, historical, snapshot, delayed, end-of-day.

### FIX APIs
Based on the Financial Information eXchange (FIX) protocol, for traders, brokers and platforms requiring high-speed, low-latency market data. Ultra-fast & reliable tick-by-tick delivery with minimal latency. (No public documentation pages — contact sales.)

## Feature matrix (from the APIs product page, verbatim)

| Feature | Detail |
|---|---|
| Streaming Live Data API | L1 data @1sec frequency with single best bid/ask details |
| Historical Data API | tick, intraday and end-of-day history |
| Live Option Chain API | Realtime data of entire option chain of the underlying in single call |
| Realtime Option Greeks API | IV, Delta, Theta, Vega, Gamma, & 16+ matrices in single call |
| Realtime Exchange Snapshot API | Live data of entire exchange segment in single call |
| Historical Exchange Snapshot API | Historical data of entire exchange segment in single call |
| Type of data | Realtime, snapshot, delayed, End-Of-Day |
| Type of APIs | WebSockets, RESTful, DotNet, COM |
| Exchanges available | NSE (Stocks, Indices, Futures & Options), NSE CDS (Currency), MCX, BSE (Stocks & Indices), BFO (BSE Futures & Options) |
| Supported OS | Virtually any OS — MS Windows, Linux, Unix |
| Supported languages | Python, Java, JavaScript, PHP, NodeJS, C-Sharp, VB.Net, C, C++ and many more |
| Medium of delivery | Desktop, Web, Mobile |
| Symbol availability | Live & historical data of any symbol traded on supported exchanges |
| Symbol formats | Long (`FUTIDX_NIFTY_25AUG2022_XX_0`), short (`NIFTY25AUG22FUT`) & continuous (`NIFTY-I`) |
| Historical data periods | Tick, 1-2-3-4-5-10-15-30 minutes, 1-2-4-6 hours, Daily, Weekly, Monthly |
| Historical data availability | Tick (7 days), 1-2-3-4min (3 months), 5-10min (4.5 months), 15-20-30min (6 months), 1-2-4-6hours (6 months), Daily-Weekly-Monthly (since 2010) |
| Live Option Chain in Excel | Realtime data of entire option chain of the underlying in single call |
| Realtime Option Greeks in Excel | IV, Delta, Theta, Vega, Gamma, & 16+ matrices in single call |
| History of Greeks in API | IV, Delta, Theta, Vega, Gamma & 16+ matrices on on-demand basis |

## API Terms & Conditions (from https://globaldatafeeds.in/apis/, verbatim)

- The Subscriber must ensure that the information received from Global Datafeeds is terminated at their end.
- As per NSE/SEBI guidelines, we do not provide data for any kind of Gaming, lottery, Virtual trading, Simulation applications. If you have any such requirements, please take approvals from NSE & SEBI first.
- Data provided by Global Datafeeds is for charting / analysis use of a single subscriber only.
- In case of any violation of the above terms, misuse or objection by the exchanges, SEBI or Laws of the Land, Global Datafeeds has the right to terminate your service without any prior notice or intimation.
- Amount once paid will not be refunded once order is placed / data is delivered. See Terms & Conditions (https://globaldatafeeds.in/terms-and-conditions), Disclaimer (https://globaldatafeeds.in/disclaimer/) and Refund Policy (https://globaldatafeeds.in/refund-policy).

## Key entry points

| Purpose | URL |
|---|---|
| API documentation root | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/ |
| API product page | https://globaldatafeeds.in/apis/ |
| API pricing | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/api-pricing/ |
| Sample code downloads | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/ |
| Free trial request | https://globaldatafeeds.in/take-a-free-trial-of-our-products/ |
| Fundamental Data docs | https://docs.globaldatafeeds.in/ |
| News API docs | https://newsdocs.globaldatafeeds.in/ |
| Release notes | https://globaldatafeeds.in/release-notes/ |
| Contact / support | https://globaldatafeeds.in/contact-us/ (Sales: +91-9071-25-55-95) |
| Downloads | https://globaldatafeeds.in/download/ |
| Terms of use | https://globaldatafeeds.in/terms-and-conditions |

## How the API docs are organized (site structure)

- **Introduction** — Introduction to APIs, Type of Data Available, Type of APIs Available, Supported OS & Languages, Supported Exchanges, Symbol Availability, Symbol List → captured in `02-introduction-getting-started.md`
- **WebSockets API** — connection, Authenticate + 25 data/metadata functions + Streaming, OptionChain, Greeks, Delayed, GainersLosers, VolumeShockers sub-APIs → `04`–`09`, `15`–`20`
- **REST API** — connection + same function set as HTTP GET endpoints → `10`–`14`, and REST variants inside `16`–`20`
- **DotNet API** / **COM API** — connection guides + downloads → `21`, `22`
- **Code Samples** — JavaScript, Python, Java, NodeJS, Postman, REST-Python → `23`, `24`
- **Documentation & Support** — API Fields Description, Symbol Naming Conventions, Diagnostic API Responses → `03`
- **Pricing & Sales / Contact / FAQs** → `25`, `26`, `27`

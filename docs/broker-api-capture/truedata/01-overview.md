# TrueData — Overview (Company, Products & Market Data API)

> Sources:
> - https://www.truedata.in/market-data-apis (fetched 2026-07-15)
> - https://www.truedata.in/products/marketdataapi (fetched 2026-07-15)
> - https://feedback.truedata.in/knowledge-base/article/getting-started-with-truedata-market-data-api (fetched 2026-07-15)

TrueData (TrueData Financial Information Pvt. Ltd.) is an **Authorised Data Vendor for NSE, BSE and MCX** (17+ years as an authorised vendor). It sells real-time + historical Indian market data via WebSocket/REST APIs, plus retail data products (Velocity plugin, Options Decoder, TrueWealth corporate data, Futprint orderflow analytics, Nison Candle Scanner, Fin Alg indicators, NinjaTrader feeds, Mutual Fund API).

- Corporate office: 309-310, 3rd Floor, Ugati Corporate Park, Opp. ICICI Bank, Kudasan, Gandhinagar, Ahmedabad (Gujarat) - 382421
- Registered office: 404, GH 22, Mansa Devi Complex, Panchkula (Haryana) - 134001
- Customer support: **+91-7304-22-44-66** (08:45-18:00 Mon-Fri, 09:30-13:00 Sat), support@truedata.in
- API sales/queries: sales@truedata.in / apisupport@truedata.in (email addresses are Cloudflare-obfuscated on site; support email confirmed in KB as apisupport-pattern)
- Telegram channel for API updates (primary announcement channel): https://t.me/truedata_ws_api
- API website landing: https://www.truedata.in/api (redirects to Market Data API product page)
- Support portal / KB / forum: https://feedback.truedata.in
- Sandbox: https://wstest.truedata.in

## What the Market Data API delivers

From https://www.truedata.in/market-data-apis:

The TrueData Market Data API provides direct, authorised access to real-time streaming and charting-ready market data from NSE, BSE, and MCX. It delivers:

- Live tick-by-tick data with ultra-low latency
- Streaming feeds for OHLC, bid/ask, volume, and open interest
- Market Depth (where available)
- Options Chain with Greeks (Delta, Theta, Vega, Gamma, etc.)
- Corporate Data APIs offering real-time insights on company disclosures

### Types of APIs & use cases (verbatim summary)

| API | Description | Ideal for |
|---|---|---|
| Real-Time Data APIs | Live, exchange-authorised tick or minute-level data with OHLC, LTP, bid/ask quotes, and open interest | Algo systems, fintech dashboards, analytics tools |
| Market Depth API (Exclusive to BSE) | Top 5 bid and ask levels, quantities, and traded volumes | Liquidity analysis, order-flow analytics |
| Options Chain API (with Greeks) | Complete live options chain with strike prices, expiries, IV, and Greeks | Options analytics, risk models, derivatives research |
| Charting Data APIs (REST) | Clean, exchange-authorised datasets compatible with charting | Market analysis tools, dashboards |
| Streaming / WebSocket APIs | Push-based real-time streams for continuous tick data delivery with ultra-low latency | Algorithmic systems, real-time visualisations |
| Reference & Symbols API | Instrument lists, expiry dates, contract specifications | Managing large symbol universes |
| Corporate Data API | Real-time corporate announcements, AI-powered analysis of disclosures, financial results & ratios, shareholding patterns, corporate actions (dividends, splits, rights, mergers), news feeds; screeners coming soon | Fintech apps, institutional data systems |
| End-of-Day (EOD) APIs | Authorised daily OHLC and volume summaries | Trend analysis, daily reporting, portfolio dashboards |

### Feature grid (from the product page)

- **Streaming Live Data API** — L1 data @1sec frequency with single best bid/ask details
- **Live Option Chain API** — realtime data of entire option chain of the underlying in a single call
- **Realtime Option Greeks API** — IV, Delta, Theta, Vega, Gamma & 16+ matrices in a single call
- **Realtime Exchange Snapshot API** — live data of an entire exchange segment in a single call
- **Type of data** — Realtime, snapshot, delayed, End-Of-Day
- **Type of APIs** — WebSockets, RESTful, DotNet, COM
- **Exchanges** — NSE (Stocks, Indices, Futures & Options), NSE CDS (Currency), MCX (plus BSE EQ/Indices/F&O per segment list)
- **Supported OS** — MS Windows, Linux, Unix (virtually any OS)
- **Symbol availability** — Live & historical data of any symbol traded on supported exchanges
- **Supported symbol formats** — Long (`FUTIDX_NIFTY_25AUG2022_XX_0`), short (`NIFTY25AUG22FUT`) & continuous (`NIFTY-I`)
- **Languages** — Python, Java, JavaScript, PHP, NodeJS, C#, VB.Net, C, C++ and more
- **Delivery** — Desktop, Web, Mobile

### Exchange segments supported (API signup form)

NSE EQ, NSE Indices, NSE F&O, NSE CDS, BSE EQ, BSE Indices, BSE F&O, MCX.

Getting-started KB note: "At this time, we are providing Real-Time Data and Historical Data via our Market Data API for NSE Equity, NSE Indices, NSE Futures & Options, NSE Currency Derivatives and MCX. The data is Level 1 from NSE & MCX which has Best Bid Ask (Default) as part of the feed."

### Data formats & latency

- **JSON**: default for live feeds and REST endpoints
- **CSV**: available for analytical outputs on request (REST `response=csv`)
- Ultra-low latency (milliseconds)

### Reliability

- 99.995% uptime through redundant data centres
- 24×7 network monitoring by TrueData NOC
- Automatic failover

### Exclusive-to-TrueData claims

- **Market Replay** — revisit market movements as they happened (India-exclusive; see 03-realtime-websocket.md → Replay)
- **Continuous Futures & Adjusted Series** — seamless long-term data continuity
- **Full-Precision Ticks** — every price movement captured

## SDKs / official libraries

| Language | Package | Link |
|---|---|---|
| Python (current) | `truedata` | https://pypi.org/project/truedata/ |
| Python (legacy) | `truedata-ws` | https://pypi.org/project/truedata-ws/ |
| Node.js | `truedata-nodejs` | https://www.npmjs.com/package/truedata-nodejs |
| C#/.NET | `TrueData-DotNet` | https://www.nuget.org/packages/TrueData-DotNet/ |

Also compatible with Java, C++, PHP using raw REST and WebSocket endpoints. Sample Python code: https://github.com/Nahas-N/Truedata-sample-code

## Onboarding flow (KB "Getting Started", verbatim steps summarised)

1. Visit the Market Data API page (https://www.truedata.in/products/marketdataapi); explore API Forum, Sandbox, Documentation, libraries.
2. Fill the signup form (entity type, purpose, segments, feed type, language, environment etc.).
3. API Team shares Documentation + Sample Codes + Libraries (Python, Nodejs, C# .NET) and Free Trial credentials. **Up to 10 days free trial; a paid trial of another 10 days possible.**
4. Test on the Sandbox (https://wstest.truedata.in) or via a library.
5. Pricing is shared based on: exchange segments, number of symbols, Real Time + Historical vs one of these.
6. FAQs: https://feedback.truedata.in/knowledge-base/faqs/market-data-api
7. KB: https://feedback.truedata.in/knowledge-base/articles/websocket-api_1
8. Full Market Feed Replay is available to all active (paid & trial) subscribers post market hours at no extra cost.

The raw-connection documentation PDF ("TrueData Market Data API Documentation v2.2") is shared by email after signup; public copies exist on Dropbox (https://www.dropbox.com/sh/cpba3vkccsfapkh/AADIBn36OaBbiu_jMXzXRQdka?dl=0 — linked from KB) and Scribd (gated).

## Compliance & usage restrictions (condensed from market-data-apis page)

- All market data remains the IP of the exchanges; data is **licensed, not owned**. Personal/internal use only; redistribution, resale or public sharing requires prior written approval from exchanges and TrueData.
- Only exchange-approved intervals may be offered: **Tick, 1-Minute, 2-Minute, 5-Minute, 15-Minute (Delayed), and End-of-Day (EOD)**.
- Prohibited (SEBI/exchange compliance): gamification/trading contests, virtual/paper trading & simulators, promotion of speculative/manipulative trading or profit claims, unregistered advisory services, real-time feeds on educational platforms (only 1-day-delayed data with exchange approval).
- TrueData logs usage; violation ⇒ immediate suspension/termination, no refund. Exchange fees (if applicable) are separate from TrueData subscription costs.
- Terms & Conditions (API order page): data must terminate at the subscriber's end; no data for Gaming/Virtual trading/Simulation per NSE/SEBI guidelines; data is for personal charting use of a single subscriber; no refund once order is placed/data delivered.

## Pricing model

No public price list for APIs. Pricing depends on exchange selection (NSE/BSE/MCX), number of symbols/instruments, and type of data (Streaming, Charting, Corporate). Contact sales@truedata.in or fill the form at https://www.truedata.in/products/marketdataapi. General pricing page: https://www.truedata.in/price.

## Product FAQs (market-data-apis page, verbatim Q list)

1. How can I get started with TrueData Market Data APIs? — fill the Market Data API form and email details to sales.
2. Can I use TrueData APIs for algorithmic systems? — Yes, for internal systems; redistribution requires exchange approval.
3. Can I use charting data for analysis? — Yes; redistribution/resale not permitted.
4. Does TrueData support multiple programming languages? — Yes: PyPI, npm, NuGet + REST/WS for others.
5. How can I get pricing details? — via the product page/sales email with requirements.
6. Is there a free trial? — Limited-period trial, subject to exchange approval & compliance review.
7. What happens if compliance rules are violated? — immediate termination, possible exchange reporting, no refund.

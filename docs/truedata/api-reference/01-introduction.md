# 01 — Introduction (vendor overview, documentation surfaces, products)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Who the vendor is

| Claim | Value | Source (access 2026-07-15) | Confidence |
|---|---|---|---|
| Company | TrueData Financial Information Pvt Ltd | Medium handle "@truedatapvtltd" (search-relay); PyPI author emails @truedata.in (direct-fetch) | high |
| Exchange authorization | "Authorized Realtime (L1) Data Vendor of NSE (Stocks, Indices, F&O & Currency), BSE (Stocks, Indices & F&O) and MCX" | truedata.in homepage/why snippets via search-relay | medium |
| Product suite | Velocity (desktop RT+historical plugin), **Market Data API** (the API product this pack documents), Options Decoder, Cloud VPS, NinjaTrader Indicators | truedata.in/products/all-products via search-relay | medium |
| Platform integrations | MS Excel, AmiBroker, NinjaTrader 7/8, MetaStock, MotiveWave, Multicharts, Advance Get, Updata, Arthachitra, Omega Research/ProSuite, "Intelligent Trading API Bridge" | feedback.truedata.in KB category list via search-relay | medium |
| Data vendor ONLY | No order routing / broking in the doc surface — pure market-data vendor | negative observation across all retrieved material | high (for the retrieved surface) |

## 2. The vendor's documentation surfaces (the real doc map)

| Surface | URL | Status 2026-07-15 | What lives there |
|---|---|---|---|
| API landing / docs portal | truedata.in/api | 403-blocked; referenced as the "Documentation" link by BOTH PyPI packages (direct-fetch) | The primary API doc portal per SDK links |
| Product pages | truedata.in/market-data-apis · /products/marketdataapi · /products/velocity · /products/all-products | 403-blocked; titles via search-relay | Marketing + the TRIAL sign-up form (on /products/marketdataapi) |
| Pricing | truedata.in/price (also /information/price) | 403-blocked; page renders pricing interactively per relay | See [07-limits-plans.md](./07-limits-plans.md) |
| **Support portal + knowledge base** | **feedback.truedata.in** | 403-blocked; extensive content via search-relay | THE support-side doc surface: knowledge base (18 categories), FAQs, forum topics, tickets, ideas board |
| Sandbox | wstest.truedata.in ("Websocket/REST JS Client") | 403-blocked; title via search-relay | Browser test client — login with API credentials, test RT + historical + EOD |
| Symbol-list downloads | truedata.in/downloads/symbol_lists/… (numbered per-segment .txt, e.g. `1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt`) | 403-blocked; URL cited in vendor KB via search-relay | Static per-segment symbol lists |
| Blog | truedata.in/blog/… (mirror: newweb.truedata.in/blog/…) | 403-blocked; fragments via search-relay | e.g. "Symbols list and format for TrueData" |
| Tutorials | truedata.in/tutorials/tutorials ("Video Tutorials") | 403-blocked | |
| Telegram | t.me/truedata_ws_api (Market Data API) · t.me/s/truedata_velocity (Velocity) | t.me blocked; channel names from PyPI/npm READMEs (high) | Vendor announcement channels |
| Official PDF | "TrueData Market Data API Documentation" v2.2.1 (Scribd mirror observed) | 403-blocked — UNRETRIEVED | The formal API doc; treat as the probe-time cross-check |
| SDK registries | PyPI `truedata` + `truedata-ws`, npm `truedata-nodejs`, NuGet `TrueData-DotNet`, github.com/Nahas-N/Truedata-sample-code | **direct-fetch OK** (full sources extracted) | The high-confidence evidence base of this pack |
| Dead link | github.com/kapilmar/truedata-ws — **404** (still listed as "Homepage" by both PyPI packages) | genuine 404, not proxy | recorded so nobody chases it |

**Postman collection:** none discovered (2026-07-15) — UNVERIFIED whether
one exists.

## 3. feedback.truedata.in knowledge-base map (search-relay, medium)

Categories: Market Data API · MS Excel · TrueData™ Velocity 2.0 · Symbol
Formats · Amibroker RTD Plugin · Amibroker RTD Local Database ·
NinjaTrader 7/8 · Advance Get · MetaStock · MotiveWave · Multicharts ·
External API · TrueData Intelligent Trading API Bridge · TrueData Options
Decoder · Updata · Arthachitra · Omega Research · ProSuite 2000i.
Index pages: `/knowledge-base` (All Articles) · `/knowledge-base/faqs` ·
`/knowledge-base/faqs/market-data-api` ·
`/knowledge-base/articles/websocket-api_1` (Market Data API group).

### Market Data API articles discovered (all 403-blocked direct; content via search-relay)

| Article | Covered in |
|---|---|
| Getting Started with TrueData Market Data API | [02](./02-authentication.md) §5 |
| Historical & Real-Time Data Availability Through Market Data API | [03](./03-websocket-realtime.md), [04](./04-historical-rest.md) |
| Historical Data Availability Through REST API | [04](./04-historical-rest.md) §5 (the DEPTH article) |
| Request/Call Limit for Historical Data (REST API) and Real Time Data (WebSocket API) | [07](./07-limits-plans.md) §1 |
| What are the fields included in Real-Time Tick Streaming? | [03](./03-websocket-realtime.md) §4 |
| Fields included in Real-Time Tick & 1 Min. Bar Streaming | [03](./03-websocket-realtime.md) §4 |
| How are the bars formed? Is the data delayed? | [09](./09-ops-misc.md) §2 |
| Full Market Feed Replay … for non-market-hours development | [03](./03-websocket-realtime.md) §7 |
| How to Fetch Real-Time Data … on the TrueData Sandbox Page | [09](./09-ops-misc.md) §5 |
| How to get EOD Data In TrueData Market Data API Sandbox Page | [09](./09-ops-misc.md) §5 |
| How to fetch Options Chain using TrueData Python Library | [05](./05-analytics-corporate.md) §4 |
| Symbol Lists & Symbol Formats for TrueData Velocity 2.0 | [06](./06-symbology.md) |
| Symbols Format for MetaStock | [06](./06-symbology.md) §2 |
| Getting Started with TrueData Options Decoder (+ Option Chain Replay, OI Graph articles) | [09](./09-ops-misc.md) §6 |
| Change Log — Velocity 2.0 | [09](./09-ops-misc.md) §7 |
| Forum: Live vs touchline data · csv headers · Symbols for index like NIFTY, BANKNIFTY | [06](./06-symbology.md) / [09](./09-ops-misc.md) |

## 4. Service architecture (triangulated; high where SDK-derived)

- **Real-time**: WebSocket push — `wss://push.truedata.in:<port>` — tick /
  1-min / 5-min bar streaming, bid-ask L1 (NSE/MCX), bid-ask L2 (BSE),
  greeks stream, market status, heartbeat → [03](./03-websocket-realtime.md).
- **Replay**: `wss://replay.truedata.in:<port>` — full-feed replay for
  non-market-hours development, free for API subscribers.
- **Historical**: REST at `https://history.truedata.in` (bearer token
  from `https://auth.truedata.in/token`) → [04](./04-historical-rest.md).
- **Symbol/utility**: `https://api.truedata.in` (getAllSymbols,
  getATMStrike, getOptionChain, getprevcloseandoi) → [06](./06-symbology.md).
- **Analytics** `https://analytics.truedata.in/api/` + **Greeks**
  `https://greeks.truedata.in/api/` + **Corporate**
  `https://corporate.truedata.in` / `wss://corp.truedata.in:9092` →
  [05](./05-analytics-corporate.md).
- **Official client libraries**: Python (`truedata`, older `truedata-ws`),
  Node.js (`truedata-nodejs`), .NET (`TrueData-DotNet`); vendor pages
  claim Java/C++ support too (search-relay, medium) → [08](./08-sdks.md).
- Legacy: historical data was once served over WebSocket — deprecated,
  "available only up to 30 Apr 2021"; REST replaced it → [09](./09-ops-misc.md) §7.

## 5. Reading order

[02-authentication.md](./02-authentication.md) (the three auth patterns),
then [03](./03-websocket-realtime.md) (the live feed — TickVault-side),
then [04](./04-historical-rest.md) (history + DEPTH — the BruteX-critical
file), then [06](./06-symbology.md) and [07](./07-limits-plans.md).

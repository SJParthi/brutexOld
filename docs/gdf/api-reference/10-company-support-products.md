# 10 — Company, authorization, support, desktop products & ops

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **Contact**, support
center, authorization pages, desktop-product pages.

## 1. Company & exchange authorization

| Claim | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Legal entity | Global Financial Datafeeds LLP ("GFDL"; brand "Global Datafeeds") | zoominfo listing + vendor branding via search-relay | medium |
| Authorization | "Authorized NSE, BSE, MCX & NCDEX Stock Realtime API Provider"; "authorized real-time data vendor of Indian Stock Exchanges NSE, NFO, CDS, MCX, BSE, BFO, NCDEX and Fundamental Data" | vendor authorization pages via search-relay | medium |
| Founded / history | "Established in 2010"; "pioneers in vendor-neutral streaming data dissemination in India" | vendor pages via search-relay | medium |
| Scale/uptime | "uptime of 99.995% [during market hours]"; "50 million data requests daily" | homepage + `/apis/` via search-relay | medium (marketing) |
| Twitter/X | @GlobalDatafeeds ("GlobalFinancialDataFeeds LLP") | x.com listing via search | medium |

## 2. Support & contacts

| Channel | Value | Source | Confidence |
|---|---|---|---|
| API developer support | developer@globaldatafeeds.in | vendor package author email (pypi-fetch) + docs relay | high |
| Sales / trial | sales@globaldatafeeds.in | docs relay ("contact … if you don't have an endpoint") | medium |
| Data-product support | support@globaldatafeeds.in | contact-details KB article relay | medium |
| Phones | +91-8928922002 · +91-9175744002 · +91-7219764002 (tech support); +91 8550 828 828 (marketing pages) | search-relay of contact pages | medium |
| Support hours | Mon–Fri 8:15–18:00, Sat 9:30–14:20, closed on stock-exchange holidays | search-relay of support pages | medium |
| Support surfaces | `/support-center/` · `/user-guide` · `/download/` · `/contact-us/` · `/articles/…` KB · docs contact page `…/contact/contact-technical-support/` | search-relay (URLs observed) | medium |
| Status/uptime page | **UNVERIFIED** (none surfaced) | — | UNVERIFIED |

## 3. Desktop feed products (non-API; context for pricing + IEOD history)

| Product | Platforms | Notes | Source/conf |
|---|---|---|---|
| NimbleDataPro | AmiBroker (32/64-bit) | entry product; plans from 5 symbols/segment | vendor `/nimbledatapro/` + compare page via search-relay; medium |
| NimbleDataProPlus | AmiBroker, MetaStock, NinjaTrader 6/7, AdvancedGet, MultiCharts, Ensign, Neuroshell Trader, Fibonacci Trader, Galactic Trader, ELWAVE, SierraChart | 225 symbols at a time; 450 when subscribing both segments (NSE FNO + MCX); "true tick-by-tick data with time-stamp of 1 second … 5 times faster than most data vendors whose data updates every 5 seconds" | compare page via search-relay; medium |
| NimbleDataProPlus Lite | lighter variant | third-party pricing page exists | low |
| NimbleKey | MTPredictor, MotiveWave, AbleTrend, Wave59, ArthaChitra, PTS Primo Charts, QuantShare, Updata, Sentient Trader, Volfort | key-based feed for third-party platforms | compare page relay; medium |
| NimbleDataProPlus VM | hosted-VM variant (reseller traderskart.in) | | low |
| Excel integration | marketed historically; current page **UNVERIFIED** | | UNVERIFIED |

## 4. IEOD / deep-intraday history packs (file products)

1-min IEOD data: NSE F&O since 2010-09-01 (back-adjusted futures format),
MCX since 2013-04-01, NSE CDS since 2014-04-07 (marketcalls relay, low).
Vendor KB: "IEOD Data Import in AmiBroker (1minute)"
(`/articles/nfo-ieod-data-import-in-amibroker/` — existence medium).
Desktop realtime DB retains ~2 months of 1-min history; subscribed-segment
1-min CSVs emailed after market close (low). Full depth discussion +
BruteX relevance: [04-historical-data.md](./04-historical-data.md) §6.

## 5. Market timings / holidays / data quality

- Support + uptime language keys off exchange market hours; a GDF-authored
  market-timings/holiday-calendar page: **UNVERIFIED** (none surfaced;
  the support-hours page says "closed on all stock exchange holidays").
- Corporate-action adjustment statements for EOD/IEOD history (beyond the
  IEOD "backadjusted futures" wording): **UNVERIFIED**.
- Runtime market status: `GetMarketMessages` (see
  [05](./05-instruments-reference.md) §3).

## 6. Research watch-outs (recorded so nobody re-trips them)

1. Name collisions: PyPI `gfdl` (LANL ML lib) ≠ GFDL; "Nimble API" web
   hits mostly belong to nimble.com CRM / nimbleway proxies.
2. `sisoppin/gdta`'s README endpoint is a fabricated placeholder domain.
3. Vendor README code samples contain typos/bugs
   ([09](./09-sdks-code-samples.md) §1) — the vendor-site PDFs are the
   cleaner samples.
4. The vendor site's `/asdf/` URL serves a stray duplicate of the APIs
   landing title — cite `/apis/` instead.
5. GDF is a DATA vendor only — no order routing/broking; any BruteX /
   TickVault use would be data-side only.

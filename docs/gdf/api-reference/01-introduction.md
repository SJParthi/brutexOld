# 01 — Introduction (vendor overview, doc portal, API families, data scope)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor section covered: **Introduction**
(`…/introduction/introduction-to-apis/`, `…/introduction/type-of-apis-available/`,
`…/introduction/type-of-data-available/`).

## 1. Who the vendor is

| Claim | Value | Source (access 2026-07-15) | Confidence |
|---|---|---|---|
| Vendor | Global Datafeeds, globaldatafeeds.in — Indian market-data vendor ("Live data of NSE Stocks, NSE F&O, NSE CDS, MCX, BSE Stocks and BSE F&O in charting platforms") | homepage title via search-relay | medium |
| Legal entity | Global Financial Datafeeds LLP ("GFDL") | zoominfo listing + vendor branding via search-relay | medium |
| Authorization claim | "Authorized NSE, BSE, MCX & NCDEX Stock Realtime API Provider"; "authorized real-time data vendor of Indian Stock Exchanges NSE, NFO, CDS, MCX, BSE, BFO, NCDEX and Fundamental Data" | `/authorised-data-vendors/`, `/authorized-nse-bse-mcx-stock-realtime-api-provider/` via search-relay | medium |
| History/scale claims | "Established in 2010"; "uptime of 99.995%"; "50 million data requests daily"; "25+ charting supported platforms" | homepage + `/apis/` via search-relay | medium (vendor marketing) |
| Data vendor ONLY | No order routing / broking anywhere in the doc surface — pure market-data vendor | negative observation across all retrieved material | high (for the retrieved surface) |

## 2. Type of APIs available (vendor section)

| API family | What it is | Source | Confidence |
|---|---|---|---|
| WebSockets API | JSON MessageType protocol — realtime streams + on-demand calls (the primary API; see [03](./03-websocket-functions.md)) | `/introduction/type-of-apis-available/` via search-relay + vendor GFDLWS package (pypi-fetch) | high (triangulated) |
| RESTful API | The SAME function vocabulary as `GET /{Function}/?accessKey=…` (see [02](./02-connect-auth.md)) | same + vendor gfdl-rest package (pypi-fetch) | high (triangulated) |
| DotNet API | .NET DLLs (x86 & x64) + docs download | `…/dotnet-api-documentation/download-2/` via search-relay | medium |
| COM API | COM component (same endpoint+port+API-key model) | `…/com-api-documentation/how-to-connect-using-com-api/` via search-relay | medium |
| Advanced Streaming API | SubscribeOptionChain / SubscribeOptionChainGreeks / SubscribeTopGainersLosers (see [06](./06-greeks-optionchain.md)) | vendor doc URLs + GFDLWS 0.0.4 modules (pypi-fetch) | high |
| Greeks API | Realtime/snapshot/history option Greeks (see [06](./06-greeks-optionchain.md)) | vendor doc URLs + GFDLWS modules (pypi-fetch) | high |
| Fundamental Data API · Stock Market News API | Marketed on the authorization page; NO endpoint documentation retrievable | `/authorised-data-vendors/` title via search-relay | medium (existence only); endpoints UNVERIFIED |

## 3. Type of data available (vendor section)

| Claim | Value | Source | Confidence |
|---|---|---|---|
| Realtime | "Realtime data updating at 1 second frequency" for NSE Stocks, NSE Indices, NSE F&O, NSE Currency, MCX, BSE Stocks, BSE Indices & BSE F&O | `/introduction/type-of-data-available/` via search-relay | medium |
| Historical periodicities | "Historical data of Periodicity Tick, Minute, Day, Week, Month" (+HOUR per the function parameter table) | same + vendor README parameter table (pypi-fetch) | high (triangulated) |
| Historical depth (bundled) | tick 7 days · 1–4 min 3 months · 5–10 min 4.5 months · 15–30 min + hourly 6 months · daily/weekly/monthly since 2010 — full table + caveats in [04-historical-data.md](./04-historical-data.md) | same page via search-relay | medium |
| Delayed-feed variant | "delayed" appears in the `/apis/` landing title; `DataDelay` is a per-exchange field in GetLimitation (0 = realtime) | search-relay + GFDLWS README (pypi-fetch) | high (the field) / medium (the sold variant) |
| Exchanges observed as API tokens | NSE, NSE_IDX, NFO, CDS, BSE, BSE_IDX, BFO, BSE_DEBT, MCX | vendor GetExchanges samples (pypi-fetch) | high |
| NCDEX | claimed at company level; NEVER observed as an API exchange token | search-relay (claim) / negative observation (token) | UNVERIFIED as API exchange |

## 4. The vendor's documentation portal — full tree

Base `https://globaldatafeeds.in/global-datafeeds-apis/…` (WordPress; two
overlapping URL trees — a "Global Datafeeds APIs" post tree and a
"documentation-support" archive tree — serve the same doc set). Observed
2026-07-15 via search-relay + vendor-package README cross-references;
no page fetched directly (environment block):

| Vendor section | Observed pages |
|---|---|
| Hub | `/global-datafeeds-apis/global-datafeeds-apis/` · `/global-datafeeds-apis/documentation-support/` · `…/documentation-support/documentation/` |
| Introduction | `…/introduction/introduction-to-apis/` · `…/introduction/type-of-apis-available/` · `…/introduction/type-of-data-available/` |
| WebSockets API | `…/websockets-api-documentation/how-to-connect-using-websockets-api/` + per-function pages (e.g. `…/function-subscriberealtime/`, `…/function-getlastquote-2/`, `…/getinstruments/`, `…/getstrikeprices-2/`) for the full function list |
| REST API | `…/rest-api-documentation/how-to-connect-using-rest-api/` + per-function pages (`…/function-gethistory/`, `…/function-getlastquote/`, `…/function-getlastquotearray/`, `…/function-getsnapshot/`, `…/getexchangesnapshot/`, `…/gethistoryaftermarket-2/`) |
| Advanced Streaming API | `…/advanced_streaming_api/subscribeoptionchain/` (section holds the Subscribe* chain/gainers functions) |
| Greeks API | `…/greeks-api/subscriberealtimegreeks/` · `…/greeks-api/getlastquoteoptiongreekschain/` · `…/greeks-api/gethistorygreeks-2/` (+ REST-side greeks archive) |
| COM API | `…/com-api-documentation/how-to-connect-using-com-api/` |
| DotNet API | `…/dotnet-api-documentation/download-2/` (docs + DLLs x86/x64) |
| Code Samples | `…/api-code-samples/{websockets-python, websockets-java, websockets-javascript-sample, websockets-nodejs, websockets-postman, rest-python, download-code-samples-2}/` |
| FAQs | `…/faqs/faqs/` |
| Pricing & Sales | `/global-datafeeds-apis/pricing-sales/` · `…/pricing-sales/api-pricing/` |
| Contact | `…/contact/contact-technical-support/` |
| Symbology | `…/documentation-support/symbol-naming-conventions/` + site KB `/articles/symbol-naming-conventions/` |

Site-level (non-API) pages: `/apis/` (API landing, "Take A Free Trial"),
`/authorised-data-vendors/` + two authorization marketing pages,
`/compare-desktop-data-products/`, `/nimbledatapro/`,
`/nimble-data-products/`, `/support-center/`, `/user-guide`, `/download/`,
`/contact-us/`, `/articles/…` knowledge base. Downloadable resources
referenced by the vendor README (not retrievable here):
`GFDL_Websocket_Python_Sample_AllFunctions.pdf` (2024-07),
`GetLastQuoteOptionChainResponse_JSON.zip`,
`GetExchangeSnapshot_1Min_JSON.zip`,
`OptionGreeksChainWithQuoteResponse_JSON.zip`.

## 5. Reading order

New to this vendor? Read [02-connect-auth.md](./02-connect-auth.md) (the
one auth model + two transports), then [03](./03-websocket-functions.md)
(the function vocabulary — everything else is a subset), then
[04](./04-historical-data.md) (history + depth — the BruteX-critical
file), then [07](./07-symbology.md) (identifier grammars) and
[08](./08-limits-entitlements-pricing.md) (what your key can actually do).

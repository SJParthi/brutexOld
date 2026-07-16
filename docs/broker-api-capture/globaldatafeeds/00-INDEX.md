# GlobalDataFeeds (GFDL) — Documentation Archive Index

Complete capture of the public GlobalDataFeeds (globaldatafeeds.in) API documentation, taken 2026-07-15. Every section notes its source URL and the site's last-modified date. See `SOURCES.md` for the full URL manifest with fetch status.

| File | Contents |
|---|---|
| `00-INDEX.md` | This map. |
| `SOURCES.md` | Every URL fetched/captured, with status (ok / partial / failed / gated). |
| `01-overview.md` | Company profile, product lines, the five API families (WebSockets, REST, DotNet, COM, FIX), feature matrix, API terms & conditions, key entry-point URLs. |
| `02-introduction-getting-started.md` | Introduction to APIs, Type of Data Available, Type of APIs Available, Supported OS & Languages, Supported Exchanges, Symbol Availability, Symbol List (downloadable ZIPs). |
| `03-fields-symbols-diagnostics.md` | Master API field reference (every response field + type + description), Symbol Naming Conventions (long/short/continuous formats), Diagnostic API Responses (plain-text error messages & handling). |
| `04-websocket-connection-auth.md` | WS connect flow (`ws://endpoint:port`), full function list, single-session rule, Authenticate request/response. |
| `05-websocket-subscribe.md` | SubscribeRealtime (incl. Unsubscribe), SubscribeSnapshot — request params, response schemas, periodicity values. |
| `06-websocket-quotes.md` | GetLastQuote, GetLastQuoteShort, GetLastQuoteShortWithClose, GetLastQuoteArray, GetLastQuoteArrayShort, GetLastQuoteArrayShortWithClose. |
| `07-websocket-snapshot-history.md` | GetSnapshot, GetHistory (periodicities, Max bars, flags), GetHistoryAfterMarket, GetExchangeSnapshot. |
| `08-websocket-instruments-metadata.md` | GetExchanges, GetInstrumentsOnSearch, GetInstruments, GetInstrumentTypes, GetProducts, GetExpiryDates, GetOptionTypes, GetStrikePrices, GetHolidays. |
| `09-websocket-server-info-messages.md` | GetServerInfo, GetLimitation (account limits), GetMarketMessages, GetExchangeMessages. |
| `10-rest-connection.md` | REST base URL/port + accessKey auth, JSON/XML response selection, handling special characters, Silverlight clientaccesspolicy, Flash crossdomain policy. |
| `11-rest-quotes.md` | REST GetLastQuote family (6 endpoints) with full URL templates, params and responses. |
| `12-rest-snapshot-history.md` | REST GetSnapshot, GetHistory, GetHistoryAfterMarket, GetExchangeSnapshot. |
| `13-rest-instruments-metadata.md` | REST GetExchanges, GetInstrumentsOnSearch, GetInstruments, GetInstrumentTypes, GetProducts, GetExpiryDates, GetOptionTypes, GetStrikePrices, GetHolidays. |
| `14-rest-server-info-messages.md` | REST GetServerInfo, GetLimitation, GetMarketMessages, GetExchangeMessages. |
| `15-streaming-api.md` | Streaming API (WS): StreamAllSymbols, StreamAllSnapshots — full-exchange push streams. |
| `16-optionchain-api.md` | OptionChain API: SubscribeOptionChain (WS), GetLastQuoteOptionChain (WS + REST). |
| `17-greeks-api-websocket.md` | Greeks API (WS): SubscribeRealtimeGreeks, SubscribeSnapshotGreeks, GetSnapshotGreeks, SubscribeOptionChainGreeks, GetLastQuoteOptionGreeks, GetLastQuoteArrayOptionGreeks, GetLastQuoteOptionGreeksChain, GetHistoryGreeks — all 16+ Greek metrics (IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR…). |
| `18-greeks-api-rest.md` | Greeks API (REST): GetHistoryGreeks, GetSnapshotGreeks, GetLastQuoteOptionGreeks, GetLastQuoteArrayOptionGreeks, GetLastQuoteOptionGreeksChain (JSON/XML/CSV examples). |
| `19-delayed-api.md` | Delayed-data API: SubscribeSnapshot/GetSnapshot/GetHistory/GetExchangeSnapshot (WS) + GetHistory/GetSnapshot/GetExchangeSnapshot (REST). |
| `20-gainers-losers-volume-shockers.md` | SubscribeTopGainersLosers, GetTopGainersLosers (WS + REST), GetVolumeShockers (WS + REST). |
| `21-dotnet-api.md` | DotNet API: how to connect, DLL/sample downloads. |
| `22-com-api.md` | COM API: how to connect, downloads. |
| `23a-code-samples-websocket-js-python.md` | Code samples downloads page + full WebSockets JavaScript and Python samples. |
| `23b-code-samples-websocket-java-nodejs-postman.md` | Full WebSockets Java, NodeJS and Postman samples/guides. |
| `24-code-samples-rest.md` | REST Python sample (full listing). |
| `25-pricing-purchase.md` | API pricing page, who can purchase. |
| `26-faq.md` | All API FAQs (16 Q&A panes). |
| `27-contact-support.md` | Contact Sales, Contact Technical Support. |
| `28-fundamental-data-api.md` | Fundamental Data API (docs.globaldatafeeds.in): auth, master list of ~30 APIs, error codes, WS connect, representative endpoint (GetCorporateAnnouncements) + complete URL index of all remaining endpoint/schema pages. Product page content included. |
| `29-news-api.md` | Stock Market News API (newsdocs.globaldatafeeds.in): auth, single-company news endpoint, webhook contract, diagnostics. Product page content included. |
| `30-market-data-widgets.md` | Market Data Widgets product page (7 widget types; served via webwidgets.globaldatafeeds.in). |
| `31-release-notes.md` | Release notes page (4 product tabs, order preserved). |
| `32-python-library-ws-gfdl.md` | Official Python WebSocket library `ws-gfdl` from PyPI — v1.1.0, all 29 functions, usage examples. |

## Notes

- **FIX API**: advertised on https://globaldatafeeds.in/apis/ but has **no public documentation** — contact sales. Recorded in `01-overview.md`.
- **Fundamental Data docs**: site has ~60 endpoint pages + 61 schema pages; the auth/API-list/error/webhook/connect pages are captured verbatim and file 28 carries a complete index of the remaining page URLs (each also retrievable as `<slug>.md`).
- `_raw/` and `_decoded/` are capture-time scratch directories (raw WP-JSON extracts); safe to delete — the `.md` files are the deliverable.
- WS docs style quirks from the source site (typos in sample JSON, e.g. stray quotes) are preserved verbatim and flagged in-file where they occur.

## Gap-fill addendum (2026-07-15): Fundamental Data & News API — full endpoint/schema capture

The coverage gap noted for file 28 is now closed. Every remaining endpoint and schema page of docs.globaldatafeeds.in (and the remaining newsdocs.globaldatafeeds.in pages) was fetched verbatim via the sites' raw-markdown export (`<url>.md`) into these new files:

| File | Contents |
|---|---|
| `28a-fundamental-endpoints-1.md` | Code Samples & API Trial, Type of Corporate Data Available, WS SubscribeCorporateAnnouncements + SubscribeFinancialResults, GetCorporateAnnouncementsCategories, GetCorporateActionsCategories, GetCorporateActions, GetResultsCalendar. |
| `28b-fundamental-endpoints-2.md` | GetFinancialResultsItems, GetFinancialResults, GetFinancialRatios, GetSectoralClassification, GetSectors, GetMei, GetIndustries, GetBasicIndustries. |
| `28c-fundamental-endpoints-3.md` | GetShpItems, GetSHP, GetSHPAdvanced, GetScripMCap, GetExchangeMCap, GetAnnualReports. |
| `28d-fundamental-endpoints-4.md` | GetCompanyData, GetBulkDeals, GetBlockDeals, GetDeliveryVolumes, GetFIIDII. |
| `28e-fundamental-endpoints-5.md` | Index Constituents (offline product), GetEODStats (deprecated), GetBhavCopyCM, GetBhavCopyFO, GetFuturesAndOptions, GetIndexDetail, GetCircuitFilterDetails. |
| `28f-fundamental-endpoints-6.md` | GetStatisticsGroupAbCompanies, GetTop5GainersLosers, GetIndexHighlights. |
| `28g-fundamental-endpoints-7.md` | GetTotalTradeHighlights, GetScripGroupTradeHighlights, GetTurnoverDetailsOfTop15ScripsofAgroup. |
| `28h-fundamental-endpoints-8.md` | EOD Statistics (GetSeriesChange, GetBannedSecurities, GetDeliverable, GetVolatality, GetStatsMCap, GetNewHL, GetCircuitBreakers) + Helper APIs (GetServerInfo, GetLimitation, GetInstruments). |
| `28i-fundamental-reference.md` | Glossary (18 request + 190 response parameter definitions), Release Notes, FAQ's. |
| `28x1-fundamental-schemas-1.md` | Schema pages 1–20 (AnnualReportItem … DeliveryVolumesRange), verbatim OpenAPI fragments. |
| `28x2-fundamental-schemas-2.md` | Schema pages 21–37 (FinResultPeriod … QeCircuitFilterDetailsResult). |
| `28x3-fundamental-schemas-3.md` | Schema pages 38–61 (QeEquityDerivativesMs … VotingFactsResponse). |
| `28z-fundamental-sources.md` | Gap-fill URL manifest: 123 URLs, 119 ok / 4 failed (3 retired FinRatio pages + 1 superseded GetAnnualReports URL — empty at source). |
| `29a-news-remaining-pages.md` | News API: API Trial, List of APIs, Multi-Company News, General News, Glossary. newsdocs site now fully captured. |

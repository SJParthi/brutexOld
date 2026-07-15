# 11 — Retrieval log (every URL attempted → outcome), 2026-07-15

The honesty backbone of this pack (Golden Rule #1/#10). Environment:
BruteX build sandbox; outbound HTTPS via agent proxy. The proxy status
endpoint confirmed `connect_rejected` ("gateway answered 403 to CONNECT —
policy denial") for every blocked host below — a 403 here is an
ENVIRONMENT block, not vendor truth.

## 1. Direct fetch attempts

| URL | Outcome |
|---|---|
| https://globaldatafeeds.in/ (+ www.) | 403-blocked (proxy CONNECT policy) |
| https://nimblerest.lisuns.com/ · https://nimblewebstream.lisuns.com/ | 403-blocked (proxy) |
| https://web.archive.org/ | 403-blocked (proxy) — archive fallback unavailable |
| https://r.jina.ai/ · https://api.allorigins.win/raw?url=… | 403-blocked (proxy) — relay fallbacks unavailable |
| https://pypi.org/ + files.pythonhosted.org | 200 (allowed direct) |
| pypi.org/pypi/{gfdlws, gfdl-rest, ws-gfdl}/json | FETCHED (vendor package metadata) |
| pypi.org/pypi/gfdl/json | FETCHED — UNRELATED package (LANL ML lib), recorded to avoid confusion |
| pypi.org/pypi/{gfdlrest, globaldatafeeds, gdfl, nimbleapi, gfdl-ws, gfdlws2}/json | 404 (names do not exist; real names are GFDLWS, ws-gfdl, gfdl-rest) |
| files.pythonhosted.org …/GFDLWS-0.0.4.tar.gz | FETCHED + extracted (README + full source read) |
| files.pythonhosted.org …/gfdl_rest-1.0.3.tar.gz | FETCHED + extracted (README + full source read) |
| raw.githubusercontent.com/sisoppin/gdta/develop/README.md | FETCHED (third-party; endpoint found to be a placeholder) |
| globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.pdf | NOT retrievable (host blocked); known via search index |
| globaldatafeeds.in/resources/{GetLastQuoteOptionChainResponse_JSON.zip, GetExchangeSnapshot_1Min_JSON.zip, OptionGreeksChainWithQuoteResponse_JSON.zip} | NOT retrievable (host blocked); URLs cited in vendor READMEs |

## 2. GitHub code search (github-fetch)

| Query | Outcome |
|---|---|
| code: `nimblewebstream.lisuns.com` | 7 hits — `ws://nimblewebstream.lisuns.com:4575` in 2 independent repos (DeltaStream-OptionAnalysis2, Stonks-API) |
| code: `nimblerest.lisuns.com` | 12 hits — `http://…:4531/` (≥4 repos) + `HTTPSConnection(…, 4532)` (karthiciist repos); full REST query-string examples captured |
| code: `"wss://nimblewebstream" OR "lisuns.com:4576"` | 0 hits — TLS WS endpoint unevidenced |
| repos: `globaldatafeeds OR gfdlws OR "global datafeeds"` | 1 repo (sisoppin/gdta); no vendor org found |

## 3. WebSearch relays (vendor URLs surfaced & summarized — never byte-read here)

All `globaldatafeeds.in` URLs below are **relay-only** evidence
(medium confidence): the homepage, `/apis/`, the authorization pages, the
whole `global-datafeeds-apis` docs tree (Introduction ×3, WebSockets API
how-to-connect + per-function pages, REST API how-to-connect +
per-function pages incl. gethistory / getlastquote(-array) / getsnapshot /
getexchangesnapshot / gethistoryaftermarket, Advanced Streaming
subscribeoptionchain, Greeks API ×3, COM/DotNet pages, Code Samples ×7,
FAQs, Pricing ×2, Contact), the symbology article (both URL forms), the
desktop-product + support pages, and the third-party pricing pages
(marketcalls / traderskart / tradingqna). ~20 search queries were issued;
the full query list lives in the session research notes.

## 4. What could NOT be verified from this environment (carried as UNVERIFIED throughout this pack)

- Production WS host/port from a VENDOR page (third-party ×2 + vendor-FAQ
  port corroboration only); any `wss://` TLS WS endpoint
- API pricing numbers; NCDEX as an API exchange token; the India VIX
  identifier; expired-contract API history; per-second rate caps;
  concurrent-connection limits; heartbeat behavior; the
  SubscribeOptionChain `Depth` semantics; corporate-action adjustment
  policy; a status page; Fundamental/News API endpoints
- Everything above needs a credentialed live probe (start with
  `GetLimitation` + `GetExchanges` + `GetInstruments('NSE_IDX')`) before
  being encoded as fact in BruteX/TickVault code.

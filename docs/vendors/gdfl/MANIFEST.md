# GDFL docs-capture manifest — every URL attempted, 2026-07-13

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> **Environment:** BruteX build sandbox; all outbound HTTPS rides a pre-configured
> egress proxy. **75 attempts total:** 63 `globaldatafeeds.in` URLs (ALL
> proxy-CONNECT-403-blocked) + 4 archive/mirror avenues (ALL blocked) + 7 PyPI JSON
> endpoints (reached: 4×HTTP 200, 3×HTTP 404) + 1 unauthenticated GitHub API curl
> (403; superseded by an authenticated GitHub search that ran successfully).
>
> **Headline:** the ENTIRE `globaldatafeeds.in` domain — every path tried, all four
> guessed subdomains, and the official PDF under `/wp-content/` — was egress-blocked
> at the sandbox proxy: `curl: (56) CONNECT tunnel failed, response 403` with
> `HTTP_CODE=000` (the CONNECT tunnel is refused; no HTTP response from the site is
> ever received — a sandbox egress-policy block, NOT a statement that the site is
> down). The WebFetch channel independently returned `HTTP 403 Forbidden` on every
> sampled URL. **Zero bytes of the live site were retrieved.** Three key URLs
> (homepage, GetHistory, API pricing) were retried — identical failure, not
> transient.
>
> **Honesty rule:** nothing from an unreachable page is summarized or reproduced
> anywhere in this archive. The "what it is" labels below derive ONLY from
> search-result page titles/snippets found during URL discovery — they identify the
> page, they do not claim its content.
>
> **Captured-file column — REPO reality:** the verbatim vendor-authored reference
> documents WERE captured (from PyPI long-descriptions) into the session scratchpad,
> but are **deliberately NOT committed** to this public repo (the SUMMARIZE-ONLY
> licensing ruling — `README.md`). The column therefore points at the committed
> OWN-WORDS summaries; the pinned PyPI URLs + SHA256 digests in `sdk/PACKAGES.md`
> re-fetch the identical originals.

## Attempt methodology

1. URL discovery: 8 web searches (`site:globaldatafeeds.in` + API/function/pricing/
   greeks/subdomain queries) + a guessed-path list ⇒ 63 candidate URLs
   (49 search-confirmed to exist, 14 guessed).
2. Every one of the 63 attempted via `curl` through the sandbox proxy (15 s
   timeout); 3 key URLs retried a second time.
3. WebFetch sampled on 4 representative URLs (homepage, sitemap.xml,
   function-gethistory, api-pricing) — all 4: HTTP 403 Forbidden, body not
   retrieved.
4. Mirror avenues (web.archive.org, archive.ph, r.jina.ai,
   timetravel.mementoweb.org) — all failed. GitHub searched (authenticated MCP) for
   an official GDFL org — zero public repositories exist. PyPI probed for the GDFL
   package family — seven official packages found and captured.

## REACHED (PyPI + GitHub search)

| URL | What it is | Result | In-repo record |
|---|---|---|---|
| https://pypi.org/pypi/ws-gfdl/json | Official GDFL package `ws-gfdl` v1.1.0 (author `Global Datafeeds <developer@globaldatafeeds.in>`); long-description = the vendor's WebSocket API reference | HTTP 200 | summarized in `api/WEBSOCKET_API.md`; digests in `sdk/PACKAGES.md` |
| https://pypi.org/pypi/gfdl-rest/json | Official GDFL package `gfdl-rest` v1.0.3 (same author); long-description = the vendor's REST API reference | HTTP 200 | summarized in `api/REST_API.md`; digests in `sdk/PACKAGES.md` |
| https://pypi.org/pypi/gfdlws/json | Official GDFL package `gfdlws` v0.0.4 (same author); the historical `pip install gfdlws` name; description is a 122-char stub | HTTP 200 | census row in `sdk/PACKAGES.md` |
| https://pypi.org/pypi/gfdl/json | PyPI package `gfdl` — **UNRELATED** (LANL "Gradient Free Deep Learning"); probed + excluded by author metadata | HTTP 200 | excluded — not GDFL |
| https://pypi.org/pypi/globaldatafeeds/json | Name probe | HTTP 404 (no such package) | — |
| https://pypi.org/pypi/gfdl-ws/json | Name probe | HTTP 404 (no such package) | — |
| https://pypi.org/pypi/rest-gfdl/json | Name probe | HTTP 404 (no such package) | — |
| GitHub repository search `globaldatafeeds` (authenticated MCP) | Official GDFL GitHub org/repo hunt | `{"total_count":0}` — GDFL has NO public GitHub repositories; libraries ship via PyPI + site downloads only | noted in `sdk/PACKAGES.md` |

The seven GDFL packages captured via the PyPI JSON API + sdists/wheels are censused
in `sdk/PACKAGES.md` (the four rows above are the discovery probes; the remaining
packages were found by grepping the full PyPI simple index).

## UNREACHABLE — live site (all 63 URLs, identical proxy egress-block)

Error for EVERY row: `curl: (56) CONNECT tunnel failed, response 403`
(`HTTP_CODE=000`). WebFetch (sampled on homepage, sitemap.xml, function-gethistory,
api-pricing): `HTTP 403 Forbidden`.

| # | URL | What it is (search-title label only) |
|---|---|---|
| 1 | https://globaldatafeeds.in/ | Homepage |
| 2 | https://globaldatafeeds.in/robots.txt | robots.txt (URL enumeration) |
| 3 | https://globaldatafeeds.in/sitemap.xml | sitemap.xml (URL enumeration) |
| 4 | https://globaldatafeeds.in/about-us/ | About Us |
| 5 | https://globaldatafeeds.in/products/ | Products overview |
| 6 | https://globaldatafeeds.in/download/ | Downloads page |
| 7 | https://globaldatafeeds.in/apis/ | APIs landing page (realtime/historical/snapshot/delayed/EOD/option-chain/greeks) |
| 8 | https://globaldatafeeds.in/asdf/ | Alias of the APIs landing page (found via search) |
| 9 | https://globaldatafeeds.in/contact-us/ | Contact Us |
| 10 | https://globaldatafeeds.in/nimblentlite/ | NimbleNTLite product (NinjaTrader 8 feed) |
| 11 | https://globaldatafeeds.in/nimbledatapluslite/ | NimbleDataPlusLite product (AmiBroker etc. feed) |
| 12 | https://globaldatafeeds.in/global-datafeeds-nsebse-mcx-authorized-data-vendor/ | Authorized-vendor page (NSE/BSE/MCX authorization) |
| 13 | https://globaldatafeeds.in/best-amibroker-datafeeds-for-realtime-tick-data/ | AmiBroker article |
| 14 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/ | API docs root archive |
| 15 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/introduction-to-apis/ | Introduction to APIs |
| 16 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/ | Type of Data Available (the HISTORICAL DEPTH page) |
| 17 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/ | Documentation & Support archive |
| 18 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/documentation/ | Documentation archive |
| 19 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/documentation/rest-api-documentation/ | REST API doc archive (index) |
| 20 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/documentation/websockets-api-documentation/ | WebSockets API doc archive (index) |
| 21 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/documentation/rest-api-documentation/greeks-api-global-datafeeds-apis/ | Greeks API doc archive (index) |
| 22 | https://globaldatafeeds.in/global-datafeeds-apis/documentation-support/api-code-samples/ | Code Samples archive (index) |
| 23 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/how-to-connect-using-rest-api/ | REST: How to Connect |
| 24 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/ | REST: GetHistory |
| 25 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/gethistoryaftermarket-2/ | REST: GetHistoryAfterMarket |
| 26 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquote/ | REST: GetLastQuote |
| 27 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getlastquotearray/ | REST: GetLastQuoteArray |
| 28 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getsnapshot/ | REST: GetSnapshot |
| 29 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/getexchangesnapshot/ | REST: GetExchangeSnapshot |
| 30 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-getexchanges/ | REST: GetExchanges |
| 31 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/how-to-connect-using-websockets-api/ | WS: How To Connect |
| 32 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-subscriberealtime/ | WS: SubscribeRealtime |
| 33 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/function-getsnapshot-2/ | WS: GetSnapshot |
| 34 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/getinstruments/ | WS: GetInstruments |
| 35 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api-global-datafeeds-apis/getlastquoteoptionchain/ | OptionChain API: GetLastQuoteOptionChain |
| 36 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreekschain/ | Greeks API: GetLastQuoteOptionGreeksChain |
| 37 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquoteoptiongreeks/ | Greeks API: GetLastQuoteOptionGreek(s) |
| 38 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/getlastquotearrayoptiongreeks/ | Greeks API: GetLastQuoteArrayOptionGreeks |
| 39 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/greeks-api/gethistorygreeks-2/ | Greeks API: GetHistoryGreeks |
| 40 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getexchangesnapshot-delayed/ | Delayed API: GetExchangeSnapshot (Delayed) |
| 41 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/api-pricing/ | API PRICING page |
| 42 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/faqs/faqs/ | API FAQs |
| 43 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/rest-python/ | Code sample: REST Python |
| 44 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-python/ | Code sample: WebSockets Python |
| 45 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-javascript-sample/ | Code sample: WebSockets JavaScript |
| 46 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-java/ | Code sample: WebSockets Java |
| 47 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/websockets-nodejs/ | Code sample: WebSockets NodeJS |
| 48 | https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/ | Code Samples / API DLLs / documentation downloads |
| 49 | https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.pdf | PDF: official all-functions WebSocket Python sample |
| 50 | https://globaldatafeeds.in/api-documentation/ | Guessed path (existence unconfirmed) |
| 51 | https://globaldatafeeds.in/nimblerest-api/ | Guessed path (existence unconfirmed) |
| 52 | https://globaldatafeeds.in/touchline-api/ | Guessed path (existence unconfirmed) |
| 53 | https://globaldatafeeds.in/nimble-websockets-api/ | Guessed path (existence unconfirmed) |
| 54 | https://globaldatafeeds.in/pricing/ | Guessed path (real pricing page is row 41) |
| 55 | https://globaldatafeeds.in/historical-data/ | Guessed path (existence unconfirmed) |
| 56 | https://globaldatafeeds.in/realtime-data/ | Guessed path (existence unconfirmed) |
| 57 | https://globaldatafeeds.in/faq/ | Guessed path (real FAQ page is row 42) |
| 58 | https://globaldatafeeds.in/contact/ | Guessed path (real page is row 9) |
| 59 | https://globaldatafeeds.in/downloads/ | Guessed path (real page is row 6) |
| 60 | https://help.globaldatafeeds.in/ | Guessed help subdomain (existence unconfirmed) |
| 61 | https://docs.globaldatafeeds.in/ | Guessed docs subdomain (existence unconfirmed) |
| 62 | https://weblink.globaldatafeeds.in/ | Guessed weblink subdomain (existence unconfirmed) |
| 63 | https://history.globaldatafeeds.in/ | Guessed history-endpoint subdomain (existence unconfirmed) |

## UNREACHABLE — archive / mirror avenues (+ the superseded GitHub curl)

| URL | What it is | Error |
|---|---|---|
| https://web.archive.org/web/2025/https://globaldatafeeds.in/ | Wayback Machine capture of the GDFL homepage | WebFetch: unable to fetch from web.archive.org; curl: `CONNECT tunnel failed, response 403` |
| https://archive.ph/newest/https://globaldatafeeds.in/ | archive.today newest capture | `CONNECT tunnel failed, response 403` |
| https://r.jina.ai/https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/ | Jina reader proxy of the depth page | `CONNECT tunnel failed, response 403` |
| http://timetravel.mementoweb.org/api/json/2025/https://globaldatafeeds.in/ | Memento aggregator lookup | `curl: (6) Could not resolve host` |
| https://api.github.com/search/repositories?q=globaldatafeeds | Unauthenticated GitHub REST search | HTTP 403 (session-scoped API block) — superseded by the successful authenticated MCP search (REACHED table) |

## MIRRORED — captured to the session scratchpad, summarized here (NOT committed verbatim)

| Scratchpad capture (ephemeral) | Source | Committed own-words summary |
|---|---|---|
| WebSocket API reference mirror (43 KB, vendor-authored long-description) | https://pypi.org/pypi/ws-gfdl/json | `api/WEBSOCKET_API.md` |
| REST API reference mirror (55 KB, vendor-authored long-description) | https://pypi.org/pypi/gfdl-rest/json | `api/REST_API.md` |
| `gfdlws` metadata stub | https://pypi.org/pypi/gfdlws/json | census row in `sdk/PACKAGES.md` |
| 7 package sdists/wheels + pypi.json + per-package API-surface notes | PyPI files API | `sdk/PACKAGES.md` (with pinned URLs + SHA256 for bit-identical re-fetch) |

## Docs the operator should MANUALLY UPLOAD (highest-value unreachable pages)

1. **https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/**
   — the historical-DEPTH page (how far back 1-second-class / 1-minute / EOD go;
   the make-or-break fact). Search snippets suggest limited intraday depth —
   UNVERIFIED; the page itself is needed.
2. **https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/pricing-sales/api-pricing/**
   — API pricing/plan tiers (snippet: plans from ₹3,000; UNVERIFIED).
3. **https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/**
   and **https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/gethistoryaftermarket-2/**
   — the live GetHistory reference (rate limits / GetLimitation values, exact depth
   per periodicity, isShortIdentifier semantics) to cross-check the PyPI-published
   copy, which may lag the site.
4. **https://globaldatafeeds.in/wp-content/uploads/2024/07/GFDL_Websocket_Python_Sample_AllFunctions.pdf**
   — the official all-functions WebSocket Python sample PDF.
5. **Licensing / redistribution terms** — no dedicated terms URL was discoverable via
   search; ask GDFL sales (sales@globaldatafeeds.in) for the subscriber agreement
   covering data storage/redistribution rights — plus the FAQs page
   (https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/faqs/faqs/)
   and the code-samples/DLL downloads page
   (https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/api-code-samples/download-code-samples-2/).

# SOURCES — TrueData documentation capture (2026-07-15)

Status legend: **OK** = fetched & archived · **PARTIAL** = fetched, limited content · **GATED** = requires login/JS/paywall, not archivable · **DEAD** = returns empty/error · **NOT-FETCHED** = discovered, intentionally skipped (marketing/duplicate)

## truedata.in (main site)

| URL | Status | Notes |
|---|---|---|
| https://www.truedata.in/market-data-apis | OK | Full marketing/compliance page → 01-overview.md |
| https://www.truedata.in/products/marketdataapi | OK | API product page + signup form fields, features, T&C → 01 |
| https://www.truedata.in/sitemap.xml | PARTIAL | Returns application/xml, tool rendered as "[binary data]" — not parseable here |
| https://www.truedata.in/api | NOT-FETCHED | Redirect alias to the products/marketdataapi page (linked as "Website" everywhere) |
| https://www.truedata.in/price | NOT-FETCHED | Velocity retail pricing; API pricing is quote-based (per 01) |
| https://www.truedata.in/products/velocity | NOT-FETCHED | Retail plugin marketing (out of API scope) |
| Downloads (installers, symbol lists .txt) | NOT-FETCHED | Direct binaries/lists; URLs archived in 06 & 12 |

## Sandbox

| URL | Status | Notes |
|---|---|---|
| https://wstest.truedata.in/ | OK | Official "WebSocket/REST JS Client" — RT port panel, auth.truedata.in login (user/password/grant_type), history.truedata.in query panel (intervals incl. Tick/EOD, DelVolume), result columns → 02/03/04/12 |
| https://wstest.truedata.in/js/site.js | DEAD | Guessed JS path — empty |

## Support portal (feedback.truedata.in) — knowledge base & FAQ

| URL | Status | Notes |
|---|---|---|
| /knowledge-base/article/getting-started-with-truedata-market-data-api | OK | → 01 |
| /knowledge-base/articles/websocket-api_1 | OK | Market Data API KB category index (9 articles, all captured) |
| /knowledge-base/faqs/market-data-api | OK | FAQ index (12 FAQs, all captured) |
| /knowledge-base/article/fields-included-in-real-time-tick-1-min-bar-streaming | OK | Tick & 1-min bar field lists → 03 |
| /knowledge-base/article/historical-real-time-data-availability-through-market-data-api | OK | Availability + limits → 04/11 |
| /knowledge-base/article/historical-data-availability-through-rest-api | OK | → 04/11 |
| /knowledge-base/article/requestscalls-limit-via-historical-and-real-time-api | OK | Rate limits, connection & symbol control → 11 |
| /knowledge-base/article/full-market-feed-replay-is-now-live-market-data-api_1 | OK | Replay hosts/ports + per-SDK snippets + Dropbox docs link → 03 |
| /knowledge-base/article/truedata-market-data-api-how-are-the-bars-formed-is-the-data-delayed | OK | Bar formation → 03 |
| /knowledge-base/article/maintenance-timings-for-truedata-market-data-api | OK | → 11 |
| /knowledge-base/article/how-to-fetch-options-chain-using-truedata-python-library | OK | → 05 |
| /knowledge-base/article/how-to-fetch-real-time-data-for-the-market-data-api-on-the-truedata-sandbox-page | OK | Sandbox RT steps (RT 8086) → 03 |
| /knowledge-base/article/how-to-get-eod-data-in-truedata-market-data-api-sandbox-page | OK | Sandbox EOD steps → 04 |
| /knowledge-base/article/how-can-we-get-multiple-symbol-data-in-one-go-for-historical-api-call | OK | → 11 |
| /knowledge-base/article/want-to-200-symbol-historical-data-in-one-go-like-live-streaming | OK | → 11 |
| /knowledge-base/article/using-threading-at-clients-end-market-data-api | OK | → 11 |
| /knowledge-base/article/at-330-pm-the-data-i-e-for-high-previous-close-etc-values-are-coming-same-as-ltp | OK | → 11 |
| /knowledge-base/article/user-already-connected-error-solution-is-force-logout | OK | Force-logout URL (api.truedata.in/logoutRequest) → 02/11 |
| /knowledge-base/article/what-are-the-fields-included-in-real-time-tick-streaming | NOT-FETCHED | FAQ duplicate of the fields article (same field list per index preview) |
| /knowledge-base/article/what-are-the-fields-included-in-real-time-1-minute-bar-data-streaming | NOT-FETCHED | FAQ duplicate (ditto) |
| /knowledge-base/article/how-to-get-eod-data-in-websocket-api | NOT-FETCHED | FAQ duplicate of the EOD sandbox article |
| /knowledge-base/article/full-market-feed-replay-is-now-live-market-data-api | NOT-FETCHED | FAQ duplicate of replay article (_1 variant captured) |
| /knowledge-base/article/how-to-fetch-real-time-data-in-market-data-api | NOT-FETCHED | FAQ duplicate of sandbox RT article |
| /knowledge-base/article/symbol_lists | OK | Velocity symbol lists + sample formats → 06 |
| /knowledge-base/article/symbols-format-for-websocket-api-velocity-3-0 | OK | WS API symbol formats → 06 |
| /knowledge-base/article/market-data-api-ws_api-symbol-lists | OK | WS API symbol master downloads → 06 |
| /knowledge-base/articles/external-api | OK | External API category index |
| /knowledge-base/article/use-the-external-api | OK | Velocity External API install/use → 12 |
| /topic/61253-get-quote-market-data-api | OK | Forum: LTP-via-n-bars staff answer → context in 04 |
| /topic/need-information-for-api, /topic/historical-tick-data-download | NOT-FETCHED | Forum threads; low marginal value vs budget |

## Package registries (official SDK docs)

| URL | Status | Notes |
|---|---|---|
| https://pypi.org/project/truedata/ | OK | Current Python SDK v7.0.1 full README → 07 |
| https://pypi.org/project/truedata-ws/ | OK | Legacy Python SDK v5.0.11 full README (58,964 chars read in full) → 08 |
| https://www.npmjs.com/package/truedata-nodejs | OK | Node SDK full README → 09 |
| https://www.nuget.org/packages/TrueData-DotNet/ | OK | .NET SDK v1.4.7 full README + versions → 10 |

## GitHub (official)

| URL | Status | Notes |
|---|---|---|
| https://github.com/Nahas-N/Truedata-sample-code | OK | Repo page + file list via api.github.com |
| …/truedata/analytics_sample_code.py (raw) | OK | TD_analytics complete surface → 05 |
| …/truedata/history_sample_code.py (raw) | OK | → 04/07 |
| …/truedata/callback_sample_code.py (raw) | OK | pushbeta:8088 → 02/07 |
| …/truedata/option_chain_sample_code.py (raw) | OK | → 05 |
| …/truedata/full_feed_sample_code.py (raw) | OK | port 9084 full_feed → 03 |
| https://github.com/kapilmar/truedata-ws (+ raw TD.py, api contents) | DEAD | Repo listed as PyPI homepage but returns empty (removed/private) |
| https://github.com/codesutras/truedata-adapter | NOT-FETCHED | Community async adapter (endorsed by TrueData; pointer only) |

## Telegram (official announcements)

| URL | Status | Notes |
|---|---|---|
| https://t.me/s/truedata_ws_api | OK | Latest page (msgs ~351–371) → 13 |
| https://t.me/s/truedata_ws_api?before=130 | OK | Msgs ~99–129: port map, v4.0.1 Q&A, replay-era → 02/13 |
| https://t.me/s/truedata_ws_api?before=99 | OK | Msgs ~73–98: REST migration, doc v2.1 + Postman, NSE CDS launch → 04/13 |

## Raw official documentation (PDF / Postman) — GATED/DEAD

| URL | Status | Notes |
|---|---|---|
| https://www.scribd.com/document/667918775/TrueData-Market-Data-API-Documentation-v-2-2-1 | GATED | "TrueData Market Data API Documentation v 2.2" PDF — Scribd JS challenge blocks fetch |
| https://www.dropbox.com/s/ftxetq7mbqjh9e7/TrueData%20Market%20Data%20API%20Documentation%20v%202.1.pdf?dl=0/1 | DEAD | Official v2.1 PDF link (from Telegram msg 92) returns empty |
| https://www.dropbox.com/s/mon2horjnx9nt97/TrueData_History_REST_API_v1.1.postman_collection.zip?dl=0 | NOT-FETCHED (binary) | Official Postman collection for History REST |
| https://www.dropbox.com/sh/cpba3vkccsfapkh/AADIBn36OaBbiu_jMXzXRQdka?dl=0 | GATED | "Entire Documentation Folder" (WebSocket docs + samples) — JS-rendered folder |
| https://www.dropbox.com/sh/xyph0b41dby8lu8/AACAwWUSatV9PjJR7mnqmW78a?dl=0 | GATED | Docs+samples folder (via Fyers community post) |
| https://www.dropbox.com/sh/675xq5vbtcfsjg5/AADfdD6VGaC-Wg20xGQ1WMr6a?dl=0 | GATED | C#.NET sample folder |

## Third-party corroboration

| URL | Status | Notes |
|---|---|---|
| https://fyers.in/community/...truedata-websocket-api-documentation-sample-code... | OK | Confirms Dropbox docs folder contents (Python w/ & w/o library, nodejs w/ auto-reconnect, C#.NET samples) |
| https://grep.app/search?q=push.truedata.in | DEAD | Empty render |

## Not found / does not exist

- `https://www.truedata.in/llms.txt`, `/llms-full.txt` — not attempted after sitemap failure; site is classic ASP.NET marketing, no docs subpath discovered by search.
- No public `api.truedata.in` docs portal (host serves utility endpoints like `/logoutRequest`).
- `analytics.truedata.in` — no public references found in any official material (analytics served via SDK/add-on).

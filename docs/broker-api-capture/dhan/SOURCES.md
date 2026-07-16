# Sources — every URL fetched for this archive

Captured 2026-07-15 via rendered-page fetch (site is mkdocs-material; raw `.md` files are NOT served — see "Failed/empty" below). All "OK" pages were captured in full with tables and code blocks intact.

## DhanHQ API v2 docs (dhanhq.co/docs/v2)

| URL | Status | Archived in |
| --- | ------ | ----------- |
| https://dhanhq.co/docs/v2/ | OK | 01-introduction.md |
| https://dhanhq.co/docs/v2/authentication/ | OK | 02-authentication.md |
| https://dhanhq.co/docs/v2/orders/ | OK | 03-orders.md |
| https://dhanhq.co/docs/v2/super-order/ | OK | 04-super-order.md |
| https://dhanhq.co/docs/v2/forever/ | OK | 05-forever-order.md |
| https://dhanhq.co/docs/v2/conditional-trigger/ | OK | 06-conditional-trigger.md |
| https://dhanhq.co/docs/v2/portfolio/ | OK | 07-portfolio.md |
| https://dhanhq.co/docs/v2/edis/ | OK | 08-edis.md |
| https://dhanhq.co/docs/v2/traders-control/ | OK | 09-traders-control.md |
| https://dhanhq.co/docs/v2/funds/ | OK | 10-funds.md |
| https://dhanhq.co/docs/v2/statements/ | OK | 11-statements.md |
| https://dhanhq.co/docs/v2/postback/ | OK | 12-postback.md |
| https://dhanhq.co/docs/v2/order-update/ | OK | 13-live-order-update.md |
| https://dhanhq.co/docs/v2/market-quote/ | OK | 14-market-quote.md |
| https://dhanhq.co/docs/v2/live-market-feed/ | OK | 15-live-market-feed.md |
| https://dhanhq.co/docs/v2/full-market-depth/ | OK | 16-full-market-depth.md |
| https://dhanhq.co/docs/v2/historical-data/ | OK | 17-historical-data.md |
| https://dhanhq.co/docs/v2/expired-options-data/ | OK | 19-expired-options-data.md |
| https://dhanhq.co/docs/v2/option-chain/ | OK | 20-option-chain.md |
| https://dhanhq.co/docs/v2/annexure/ | OK | 18-annexure.md |
| https://dhanhq.co/docs/v2/instruments/ | OK | 21-instruments-scrip-master.md |
| https://dhanhq.co/docs/v2/releases/ | OK | 22-releases.md |

The sidebar contains exactly the 22 pages above (Introduction, Authentication, 11 Trading API pages, 6 Data API pages, Annexure, Instrument List, Releases). No IPO or pricing page exists in the v2 docs sidebar; Data API pricing is referenced as "mentioned on the platform" (see 02-authentication.md → Eligibility).

## Python SDK

| URL | Status | Archived in |
| --- | ------ | ----------- |
| https://pypi.org/project/dhanhq/ | OK (v2.2.0, Apr 24 2026) | 23-python-sdk.md |
| https://raw.githubusercontent.com/dhan-oss/DhanHQ-py/main/README.md | OK | 23-python-sdk.md, 24-python-sdk-websockets.md |
| https://dhanhq.co/docs/DhanHQ-py/ | OK (home, v2.1.0-era) | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/dhanhq/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/init/ | OK (duplicate of dhanhq/) | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/dhan_context/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/dhan_http/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_order/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_super_order/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_forever_order/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_funds/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_portfolio/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_security/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_statement/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_trader_control/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_historical_data/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_market_feed/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/_option_chain/ | OK | 23-python-sdk.md |
| https://dhanhq.co/docs/DhanHQ-py/marketfeed/ | OK | 24-python-sdk-websockets.md |
| https://dhanhq.co/docs/DhanHQ-py/orderupdate/ | OK | 24-python-sdk-websockets.md |
| https://dhanhq.co/docs/DhanHQ-py/fulldepth/ | OK | 24-python-sdk-websockets.md |

Not fetched (redundant with captured pages): https://dhanhq.co/docs/DhanHQ-py/getting_started/ (duplicates README quickstart already archived).

## Failed / empty probes (recorded, nothing fabricated)

| URL | Result |
| --- | ------ |
| https://dhanhq.co/docs/v2/_sidebar.md | Empty response — site is mkdocs-material, not docsify; no raw sidebar file |
| https://dhanhq.co/docs/v2/llms.txt | Empty response — not provided |
| https://dhanhq.co/docs/v2/orders.md | Empty response — raw markdown files not served by mkdocs |

(llms-full.txt / sitemap.xml probes were skipped once the full sidebar was obtained from the rendered index page — the sidebar is embedded in every page and was cross-checked on all 22 fetches.)

## Referenced data/tool URLs (not doc pages, listed for completeness)

- `https://api.dhan.co/v2/` — Developer Kit / Swagger explorer (interactive; not archivable as markdown)
- `https://images.dhan.co/api-data/api-scrip-master.csv` — compact scrip master CSV (data file, ~multi-MB, refreshed daily; format documented in 21-instruments-scrip-master.md)
- `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` — detailed scrip master CSV
- `https://api.dhan.co/v2/instrument/{exchangeSegment}` — segmentwise instrument list endpoint
- `wss://api-feed.dhan.co` · `wss://api-order-update.dhan.co` · `wss://depth-api-feed.dhan.co/twentydepth` · `wss://full-depth-api.dhan.co/twohundreddepth` — WebSocket endpoints (documented in files 13/15/16)

## Known quirks preserved verbatim (source-site typos, flagged inline in the archive)

- EDIS endpoint table shows `/edis/from`; the sample request uses the correct `/edis/form` (08-edis.md).
- Fund Limit response field is misspelled `availabelBalance` in the actual API (10-funds.md).
- Multi-order margin sample cURL contains stray `%20%20` in the URL (10-funds.md).
- Forever Order list: endpoint table says `GET /forever/orders`, sample cURL uses `GET /forever/all` (05-forever-order.md).
- Option Chain parameter table misspells `UnderlyingScri` (JSON key is `UnderlyingScrip`) (20-option-chain.md).
- Postback parameter table misspells `omserroeCode` (12-postback.md).
- SDK reference pages label some constant groups oddly (e.g. `INDEX = 'IDX_I'` captioned "Constants for Transaction Type") — captured as printed (23-python-sdk.md).

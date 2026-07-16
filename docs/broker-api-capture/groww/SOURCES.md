# SOURCES — Groww Trading API documentation capture

Captured: 2026-07-15. Fetch tool: web_fetch (server-side render of the Next.js docs site returned full content; no JS-shell fallbacks like sitemap.xml or llms.txt were needed).

Status legend: **ok** = fetched in full, content captured; **n/a** = page does not exist in that variant.

## Docs landing

| URL | Status | Captured in |
| --- | --- | --- |
| https://groww.in/trade-api/docs | ok | 01-introduction.md |

## cURL docs variant

| URL | Status | Captured in |
| --- | --- | --- |
| https://groww.in/trade-api/docs/curl | ok | 01, 02, 15, 16 |
| https://groww.in/trade-api/docs/curl/instruments | ok | 03-instruments.md |
| https://groww.in/trade-api/docs/curl/orders | ok | 04-orders.md |
| https://groww.in/trade-api/docs/curl/smart-orders | ok | 05-smart-orders.md |
| https://groww.in/trade-api/docs/curl/portfolio | ok | 06-portfolio.md |
| https://groww.in/trade-api/docs/curl/margin | ok | 07-margin.md |
| https://groww.in/trade-api/docs/curl/live-data | ok | 08-live-data.md |
| https://groww.in/trade-api/docs/curl/historical-data | ok | 09-historical-data.md |
| https://groww.in/trade-api/docs/curl/backtesting | ok | 10-backtesting.md |
| https://groww.in/trade-api/docs/curl/user | ok | 12-user.md |
| https://groww.in/trade-api/docs/curl/annexures | ok | 13-annexures.md |
| https://groww.in/trade-api/docs/curl/changelog | ok | 17-changelog.md |
| (Feed page) | n/a — no cURL/WebSocket-protocol variant exists | 11-websocket-feed.md notes this |
| (Exceptions page) | n/a — Python-SDK-only section | 16-errors-exceptions.md |

## Python SDK docs variant

| URL | Status | Captured in |
| --- | --- | --- |
| https://groww.in/trade-api/docs/python-sdk | ok | 01, 02, 15 |
| https://groww.in/trade-api/docs/python-sdk/instruments | ok | 03-instruments.md |
| https://groww.in/trade-api/docs/python-sdk/orders | ok | 04-orders.md |
| https://groww.in/trade-api/docs/python-sdk/smart-orders | ok | 05-smart-orders.md |
| https://groww.in/trade-api/docs/python-sdk/portfolio | ok | 06-portfolio.md |
| https://groww.in/trade-api/docs/python-sdk/margin | ok | 07-margin.md |
| https://groww.in/trade-api/docs/python-sdk/live-data | ok | 08-live-data.md |
| https://groww.in/trade-api/docs/python-sdk/historical-data | ok | 09-historical-data.md |
| https://groww.in/trade-api/docs/python-sdk/backtesting | ok | 10-backtesting.md |
| https://groww.in/trade-api/docs/python-sdk/feed | ok | 11-websocket-feed.md |
| https://groww.in/trade-api/docs/python-sdk/user | ok | 12-user.md |
| https://groww.in/trade-api/docs/python-sdk/annexures | ok | 13-annexures.md |
| https://groww.in/trade-api/docs/python-sdk/exceptions | ok | 16-errors-exceptions.md |
| https://groww.in/trade-api/docs/python-sdk/changelog | ok | 17-changelog.md |

## External

| URL | Status | Captured in |
| --- | --- | --- |
| https://pypi.org/project/growwapi/ | ok | 14-python-sdk.md |
| https://growwapi-assets.groww.in/instruments/instrument.csv | not fetched (large data file; URL + column schema documented) | 03-instruments.md |

## Referenced-but-gated / account-only pages (not documentation)

| URL | Status | Notes |
| --- | --- | --- |
| https://groww.in/user/profile/trading-apis | gated (requires Groww login) | Subscription purchase + access-token management UI |
| https://groww.in/trade-api/api-keys | gated (requires Groww login) | Groww Cloud API Keys page — key/TOTP generation & daily approval UI |

## Coverage notes

- Both code-sample variants captured for every section that exists in both (cURL + Python).
- Sections present in only one variant: **Feed** (WebSocket streaming) and **Exceptions** exist only in the Python SDK docs; the raw WebSocket wire protocol (NATS/protobuf) is not publicly documented by Groww — it is accessed via the `GrowwFeed` SDK class.
- The docs sidebar has no FAQ or separate announcements page; changelogs serve that role.
- Known inconsistencies in the source docs are preserved and flagged inline (e.g., order-list max page_size 100 vs 25; commodity support wording between cURL and Python intros; basket margin FNO+COMMODITY vs FNO-only).

# DhanHQ API v2 — Offline Documentation Archive

> Complete capture of https://dhanhq.co/docs/v2/ (DhanHQ Ver 2.0 / API Documentation, mkdocs-material site) plus the official Python SDK (dhanhq v2.2.0 — PyPI, GitHub README, and the full class reference at https://dhanhq.co/docs/DhanHQ-py/).
> Captured: 2026-07-15. Docs site version at capture time: **API v2.5** (release notes dated Feb 09 2026).
> Base REST URL: `https://api.dhan.co/v2/` · Auth base: `https://auth.dhan.co` · All REST requests need header `access-token: <JWT>`.

## Files (mirroring the site sidebar)

| File | Site page | Contents |
| ---- | --------- | -------- |
| [01-introduction.md](./01-introduction.md) | Introduction (`/docs/v2/`) | Getting started, request structure (REST + Python), error envelope, **rate-limit table** |
| [02-authentication.md](./02-authentication.md) | Authentication | Access token (24h), Generate Token via TOTP, Renew Token, API key & secret 3-step OAuth, Partner 3-step flow, **Static IP set/modify/get**, TOTP setup, User Profile |
| [03-orders.md](./03-orders.md) | Orders | Place/modify/cancel, slicing, order book, get by id/correlation id, trade book, trades of an order — full request/response field tables |
| [04-super-order.md](./04-super-order.md) | Super Order | Place/modify/cancel legs (entry/target/SL + trailing jump), super order list with legDetails |
| [05-forever-order.md](./05-forever-order.md) | Forever Order | GTT single & OCO create/modify/delete, all-forever list |
| [06-conditional-trigger.md](./06-conditional-trigger.md) | Conditional Trigger | `/alerts/orders` CRUD — price/technical-indicator triggered orders |
| [07-portfolio.md](./07-portfolio.md) | Portfolio and Positions | Holdings, positions, convert position, **Exit All Positions** |
| [08-edis.md](./08-edis.md) | EDIS | T-PIN generation, CDSL eDIS form, status & inquiry |
| [09-traders-control.md](./09-traders-control.md) | Trader's Control | Kill switch manage/status, **P&L based exit** configure/stop/get |
| [10-funds.md](./10-funds.md) | Funds & Margin | Margin calculator (single + multi order), fund limit |
| [11-statements.md](./11-statements.md) | Statement | Ledger report, trade history (paginated, with charges breakdown) |
| [12-postback.md](./12-postback.md) | Postback | Webhook payload structure + setup rules |
| [13-live-order-update.md](./13-live-order-update.md) | Live Order Update | `wss://api-order-update.dhan.co` — login JSON (SELF/PARTNER), full order_alert field table |
| [14-market-quote.md](./14-market-quote.md) | Market Quote | REST `/marketfeed/ltp`, `/marketfeed/ohlc`, `/marketfeed/quote` (1000 instruments/request) |
| [15-live-market-feed.md](./15-live-market-feed.md) | Live Market Feed | `wss://api-feed.dhan.co` — subscription JSON, **binary byte-offset tables**: header, ticker, prev close, quote, OI, full packet + 5-level depth, disconnect codes |
| [16-full-market-depth.md](./16-full-market-depth.md) | Full Market Depth | 20-level (`wss://depth-api-feed.dhan.co/twentydepth`) & 200-level (`wss://full-depth-api.dhan.co/twohundreddepth`) binary layouts |
| [17-historical-data.md](./17-historical-data.md) | Historical Data | `/charts/historical` (daily) & `/charts/intraday` (1/5/15/25/60 min, 90-day window) |
| [18-annexure.md](./18-annexure.md) | Annexure | **All enums**: exchange segments (string+numeric), product/order/AMO/expiry/instrument types, feed request & response codes, **DH-90x trading errors**, **800-series data errors**, conditional-trigger enums |
| [19-expired-options-data.md](./19-expired-options-data.md) | Expired Options Data | `/charts/rollingoption` — 5 years of expired contract data, ATM-relative strikes |
| [20-option-chain.md](./20-option-chain.md) | Option Chain | `/optionchain` + `/optionchain/expirylist` with greeks/IV/OI (1 req / 3 s) |
| [21-instruments-scrip-master.md](./21-instruments-scrip-master.md) | Instrument List | api-scrip-master.csv (compact) & -detailed.csv URLs, segmentwise endpoint, **full column description table** |
| [22-releases.md](./22-releases.md) | Releases | Release notes v2 → v2.5 (features, improvements, breaking changes) |
| [23-python-sdk.md](./23-python-sdk.md) | (PyPI/GitHub/DhanHQ-py docs) | dhanhq v2.2.0 install, DhanLogin auth flows, DhanContext/DhanHTTP, and **every REST method with parameter tables** (Order, SuperOrder, ForeverOrder, Portfolio, Funds, Statement, TraderControl, Security/EDIS, HistoricalData, MarketFeed quote, OptionChain) |
| [24-python-sdk-websockets.md](./24-python-sdk-websockets.md) | (DhanHQ-py docs) | MarketFeed, OrderUpdate, FullDepth websocket helper classes — all methods + official usage examples |
| [SOURCES.md](./SOURCES.md) | — | Every URL fetched with status |

### Numbering notes
- File numbers follow the live sidebar order except: Annexure is fixed at `18` and Instrument List at `21` (cross-references throughout the archive point to those names).
- Rate limits live in `01-introduction.md#rate-limit`; error-code tables live in `18-annexure.md` (Trading API Error DH-901…DH-910, Data API Error 800…814). There is no separate rate-limits/errors page on the site.
- The site has no separate "20-Market-Depth" page — 20-level and 200-level depth share the "Full Market Depth" page (file 16).

## Quick facts

- **Auth header**: `access-token: <JWT>` on every request; Market Quote & Option Chain additionally need `client-id` header.
- **Token validity**: 24 hours (since v2.4); API key & secret valid 12 months; renew via `POST /v2/RenewToken`.
- **Static IP whitelisting** is mandatory for all order placement/modification/cancellation APIs (Orders, Super, Forever).
- **Rate limits**: Order APIs 10/s, 250/min, 1000/h, 7000/day (25 modifications/order cap); Data APIs 5/s, 100000/day; Quote APIs 1/s; Option Chain 1 per 3 s; Non-trading 20/s.
- **WebSockets**: up to 5 connections/user, 5000 instruments/connection (100 per subscribe message) on market feed; 50 instruments for 20-depth; 1 instrument for 200-depth. Binary payloads are little-endian.
- **Scrip master**: `https://images.dhan.co/api-data/api-scrip-master.csv` (compact) / `api-scrip-master-detailed.csv` (detailed); segmentwise: `GET https://api.dhan.co/v2/instrument/{exchangeSegment}`.

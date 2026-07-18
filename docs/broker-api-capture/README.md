# Broker & Market-Data API Documentation Archive

Complete offline capture of the official API documentation of four Indian brokers / market-data vendors, taken **2026-07-15**. Built to be dropped into the `brutex` and `tickvault` repositories so the live doc sites never need to be consulted again.

Every content file carries its source URL(s) at the top. Content is verbatim — endpoint paths, parameter tables, request/response JSON, binary packet byte-layouts, enums, error codes, rate limits, and SDK references are preserved exactly as published (including the source sites' own typos, flagged inline). Nothing is fabricated; anything not publicly documented is explicitly recorded as gated/missing.

## Contents

| Folder | Provider | Files | Covers |
|---|---|---|---|
| `dhan/` | DhanHQ API v2 (dhanhq.co/docs/v2) | 26 | Auth, orders, super/forever orders, conditional trigger, portfolio, EDIS, trader's control, funds, statements, postback, live order update WS, live market feed WS (full binary byte-offset tables), 20/200-level depth, market quote REST, option chain, historical + expired options data, annexure (all enums, DH-901…910 + 800–814 error codes), scrip master CSV schema, releases v2→v2.5, full `dhanhq` Python SDK (v2.2.0, all classes/methods) |
| `groww/` | Groww Trade API (groww.in/trade-api/docs) | 19 | Auth (token API, TOTP + checksum flows with code in 4 languages), instruments CSV schema, orders, smart orders (GTT/OCO), portfolio, margin, live data (quote/LTP/OHLC/depth/option chain + Greeks), websocket feed (GrowwFeed subscriptions + message formats), historical + backtesting endpoints, user detail, all enums, rate limits, GA000–GA007 errors + exception classes, `growwapi` Python SDK (v1.5.0), changelog. Both cURL and Python variants captured |
| `truedata/` | TrueData (truedata.in) | 15 | WS auth + official port map (8082–8088/9084/replay), realtime WS (19-field tick table, bar/bid-ask/greeks messages), history REST (`history.truedata.in` full endpoint surface + param/enum tables), analytics/option chain/greeks, symbol grammar + all master-list URLs, `truedata` v7 + legacy `truedata-ws` Python SDKs, Node.js + .NET SDKs, Velocity COM API, limits/errors/FAQs, official announcements |
| `globaldatafeeds/` | GlobalDataFeeds / GFDL (globaldatafeeds.in) | 51 | All ~105 Market Data API KB pages: WS API (connect/auth, subscribe, all GetLastQuote variants, snapshot/history, instruments/metadata, server info), full REST API mirror, streaming, option chain, Greeks (WS+REST), delayed, gainers/losers, .NET/COM APIs, full JS/Python/Java/NodeJS/Postman/REST samples, pricing, FAQs; **plus the entire Fundamental Data API** (docs.globaldatafeeds.in — all ~53 endpoint pages, all 61 OpenAPI schemas, glossary of 208 params, files `28*`) and the full News API (`29*`), widgets, release notes, `ws-gfdl` PyPI library |

Each folder has `00-INDEX.md` (file map) and `SOURCES.md` (every URL fetched with ok/failed/gated status; GFDL gap-fill manifest in `28z-fundamental-sources.md`).

## Verification

Produced by 4 parallel crawler agents + 1 gap-fill agent, then audited by an independent read-only agent that (a) structurally checked all files (no empty files, no unclosed code fences, no broken tables, no truncation artifacts, INDEX↔disk match), (b) ran substance checks (binary packet tables, auth flows, param tables present), and (c) diffed sampled pages against the live sites — byte-for-byte matches, zero fabricated content. Verdicts: Dhan PASS, Groww PASS, TrueData PASS, GFDL PASS (index gap fixed).

## Known gaps (not publicly available — recorded, not fabricated)

- **GFDL FIX API**: advertised, docs are contact-sales only. 3 retired FinRatio endpoint pages + 1 superseded AnnualReports URL are empty at source.
- **TrueData raw WS wire-spec PDF** ("Market Data API Documentation v2.1/v2.2"): emailed to customers only — exact raw subscribe-request JSON key names live there. Request from apisupport@truedata.in. (SDK-level protocol is fully documented here.)
- **Groww raw websocket wire protocol** (NATS + protobuf schemas): not publicly documented; use the `GrowwFeed` class (fully documented).
- **Dhan Developer Kit** (api.dhan.co/v2 Swagger UI): interactive tool, not archivable; all endpoints captured from the docs instead.
- Live data files (scrip masters / instruments CSVs) not snapshotted — download URLs + full column schemas documented.
- `globaldatafeeds/_raw/` and `_decoded/`: crawler scratch copies (raw HTML). Harmless backups; keep or delete.

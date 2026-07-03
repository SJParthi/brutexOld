# Groww Trade API — Complete Documentation Pack (Python SDK + REST)

> **Captured:** 2026-07-03 directly from https://groww.in/trade-api/docs/python-sdk (+ REST section)
> **Method:** Every page in the docs navigation fetched raw, `<article>` content extracted, converted to Markdown.
> **Verification (scripted, per page):** heading count, table count, and code-block count in the Markdown were compared against the live HTML for **all 26 pages — 100% match, zero loss**. Docs are not in Groww's sitemap; the in-page navigation (14 SDK + 12 REST pages) is the complete set, and all 26 are here.
> **SDK version at capture:** `growwapi` **1.5.0** (released 3rd Dec 2025 — latest per changelog)

---

## File Index

| # | File | Contents |
|---|------|----------|
| 01 | introduction | Getting started, API key/auth |
| 02 | orders | Place / Modify / Cancel / Status / Order list / Trade list |
| 03 | smart-orders | **GTT + OCO** — create/modify/cancel, CASH + FNO (not COMMODITY) |
| 04 | portfolio | Holdings + Positions (incl. `realised_pnl`) |
| 05 | margin | User margin, required-margin calculator |
| 06 | live-data | REST snapshots: Quote / LTP / OHLC |
| 07 | feed | **WebSocket streaming (now officially documented)** — `GrowwFeed`: subscribe LTP / index / market depth / order updates / position updates, sync + async callback modes |
| 08 | historical-data | Candle data |
| 09 | instruments | instrument.csv (`https://growwapi-assets.groww.in/instruments/instrument.csv`), exchange tokens, DataFrame helpers |
| 10 | user | User profile |
| 11 | backtesting | Backtesting guide |
| 12 | exceptions | SDK exception classes |
| 13 | annexures | All enums: exchange, segment, product, order type, validity, statuses |
| 14 | changelog | v0.0.1 → v1.5.0 full history |
| 15–26 | BONUS-REST-* | The parallel REST/curl docs: raw endpoints behind the SDK, auth flows (Access Token / API Key+Secret / TOTP), **checksum generation (SHA-256 of secret+epoch)**, rate limits, error codes |

## Key facts captured (verbatim from docs)

- **Feed subscription cap — now official:** "You can subscribe for up to **1000 instruments at a time**." ⚠️ Wording is "at a time" — the docs still do **not** state per-connection vs per-account, and do not mention multi-connection support. The core migration question remains officially unresolved; only the number is now confirmed.
- **Rate limits (type-level, shared within type):** Orders 10/sec · 250/min | Live Data 10/sec · 300/min | Non-Trading 20/sec · 500/min.
- **Feed data model:** LTP payloads carry `tsInMillis` + `ltp` (snapshot-style values); market depth = 5-level buy/sell book. Order + position update streams included.
- **MCX contradiction in Groww's own docs:** Changelog v1.5.0 says "Added: Commodity trading support on MCX exchange," but the REST intro still says commodities (MCX) "is not available at this time," and Smart Orders page says COMMODITY segment not supported. Treat MCX as unverified — confirm with Groww support before relying on it.
- **Auth:** three approaches — direct Access Token, API Key + Secret (checksum flow), API Key + TOTP.


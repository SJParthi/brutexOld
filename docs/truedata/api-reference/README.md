---
title: TrueData Market Data APIs — Local Documentation Reference
researched_at: 2026-07-15
vendor: TrueData Financial Information Pvt Ltd — truedata.in
purpose: Complete offline reference of TrueData's public API documentation surface, for BruteX + TickVault vendor evaluation (DEC-GDF-TRUEDATA-DOCS-001, MIGRATION.md #1795)
authority: RESEARCH-GRADE reference (relay/package-evidenced), NOT probe-verified. Unlike docs/groww/api-reference/ (the live vendor's authoritative pack), NOTHING in this directory may be encoded as fact in code without a credentialed live probe + a new DEC. Confidence stamps per claim are binding.
---

# TrueData Market Data APIs — Local Documentation Reference

Complete research capture of **TrueData's** public documentation
(truedata.in — Indian market-data vendor; NSE / BSE / MCX real-time +
historical data), researched **2026-07-15** for BruteX and TickVault.
Vendor self-description: "Authorized NSE, BSE & MCX Market Data Vendor"
(homepage title, search-relay).

## Retrieval honesty (Golden Rule #1/#10 — read before citing anything)

Every `*.truedata.in` host (`www`, `feedback`, `wstest`, `auth`,
`history`) was **CONNECT-403-blocked** by this build environment's egress
proxy — a 403 here is an ENVIRONMENT block, not vendor truth. Evidence
channels actually used, and the per-claim confidence legend used across
ALL files in this directory:

| Method | Meaning | Confidence cap |
|---|---|---|
| `pkg-source` / `direct-fetch` | **Vendor-published SDK packages + registry pages fetched directly**: PyPI `truedata` 7.0.1 (+ `truedata-ws` 5.0.11), npm `truedata-nodejs` 1.2.0 (full source extracted), NuGet `TrueData-DotNet` 1.4.7, the vendor-staff sample repo github.com/Nahas-N/Truedata-sample-code. Vendor-published code = vendor truth for endpoints + wire protocol. | **high** |
| `search-relay` | WebSearch server-side retrieval of vendor pages (truedata.in + feedback.truedata.in knowledge base), content relayed, never byte-read here | **medium** |
| third-party only | tutorials/mirrors | **low** |
| — | Could not retrieve | **UNVERIFIED** |

An OFFICIAL API PDF exists ("TrueData Market Data API Documentation",
v2.2.1 seen mirrored on Scribd) — NOT retrievable here; response JSON
samples for the history REST are reconstructed from vendor SDK parsing
code, not captured wire bytes. A credentialed live probe is MANDATORY
before any figure here is encoded in BruteX/TickVault code. Full
URL-by-URL attempt log: [10-retrieval-log.md](./10-retrieval-log.md).

## File index (mapped to the vendor's real doc surfaces)

TrueData's documentation lives across: the marketing/API site
(truedata.in/api + product pages), the **feedback.truedata.in knowledge
base** (the real support-side doc surface — 18 categories), the
**wstest.truedata.in sandbox**, the official SDKs (Python/Node/.NET), and
the official PDF. This pack covers every surface:

| File | Covers |
|---|---|
| [01-introduction.md](./01-introduction.md) | Vendor overview, the full doc-surface map (site, knowledge base + article list, sandbox, PDF, Telegram), product suite, host architecture |
| [02-authentication.md](./02-authentication.md) | Token endpoint (OAuth-style password grant), bearer usage, WS query-string auth, api.truedata.in plain-param auth, sandbox + trial |
| [03-websocket-realtime.md](./03-websocket-realtime.md) | Real-time WebSocket — hosts/ports, commands, every message shape (touchline, tick, bid-ask L1/L2, 1-min/5-min bars, greeks, full feed), heartbeats, replay, compression |
| [04-historical-rest.md](./04-historical-rest.md) | Historical REST — every endpoint, time formats, intervals, params, response/error shapes, historical DEPTH, rate limits, segments |
| [05-analytics-corporate.md](./05-analytics-corporate.md) | Analytics + Greeks hosts & endpoints, utility API (api.truedata.in), live option-chain feature, corporate-data REST + WS |
| [06-symbology.md](./06-symbology.md) | Symbol master endpoints, naming conventions (spot/index/futures/options), segment vocabulary, symbol-ids |
| [07-limits-plans.md](./07-limits-plans.md) | Rate limits, connection/symbol limits, plans, pricing model, trial, add-ons |
| [08-sdks.md](./08-sdks.md) | Official SDKs — Python `truedata` + `truedata-ws`, Node `truedata-nodejs`, .NET `TrueData-DotNet`, sample repo, changelogs |
| [09-ops-misc.md](./09-ops-misc.md) | Company/authorization, bar formation & data-quality statements, market status/calendar, support channels, Options Decoder, changelog/migration history |
| [10-retrieval-log.md](./10-retrieval-log.md) | Every URL attempted → outcome (the honesty backbone) |

## Quick reference card

### Architecture in one paragraph (high confidence — vendor SDK source)

ONE credential pair (`login_id` + `password`) drives everything.
**Real-time** = WebSocket push at `wss://push.truedata.in:<port>` with
credentials in the query string (commands `addsymbol` / `removesymbol` /
`getmarketstatus`; tick + 1-min/5-min bar + bid-ask + greeks streams;
replay twin at `wss://replay.truedata.in`). **Historical** = REST at
`https://history.truedata.in` under a bearer token minted from
`POST https://auth.truedata.in/token` (password grant, ~1 h TTL).
**Utility** (`api.truedata.in` — symbol master, ATM strike, prev
close/OI) takes user+password as plain query params. **Analytics**
(`analytics.truedata.in`) + **Greeks** (`greeks.truedata.in`) +
**Corporate** (`corporate.truedata.in` REST, `wss://corp.truedata.in:9092`
WS) ride the same bearer/query patterns.

### Hosts & ports (vendor SDK source, high)

| Surface | Endpoint |
|---|---|
| Auth | `POST https://auth.truedata.in/token` |
| Live WS | `wss://push.truedata.in:8082` (older default) / `:8084` (v7 SDK default, "if authorized") |
| Full-market feed | vendor sample uses port `9084` (`full_feed=True`) |
| Replay WS | `wss://replay.truedata.in:<port>` |
| Corporate WS | `wss://corp.truedata.in:9092` |
| History REST | `https://history.truedata.in` |
| Utility | `https://api.truedata.in` |
| Analytics / Greeks | `https://analytics.truedata.in/api/` / `https://greeks.truedata.in/api/` |
| Corporate REST | `https://corporate.truedata.in` |
| Sandbox | `https://wstest.truedata.in` (browser WS/REST test client) |

### Load-bearing numbers (with confidence)

| Fact | Value | Confidence |
|---|---|---|
| Historical depth defaults | tick = last **5 trading days** · intraday bars (1–60 min) = last **6 months** · EOD = **10+ years**; longer = paid "**Extended History**" add-on (depth/price UNVERIFIED) | medium (vendor KB, search-relay; tick 5-day also in npm README = high) |
| REST rate limits | tick 5/s · 300/min · 18,000/h; bars 10/s · 600/min · 18,000/h | medium (KB relay, twice-triangulated) |
| WS connections | 1 login instance at a time; symbols capped per PLAN (counts UNVERIFIED) | medium |
| History time format | `YYMMDDTHH:MM:SS` (2-digit year); intervals 1/2/3/5/10/15/30/60 min + EOD (+week/month) | high (SDK source) |
| Token TTL | ~3600 s (SDKs re-mint with a 15–20 s early buffer) | high (client-side evidence) |
| Rate-limit breach signals | history: body `API request quota exceeded!`; analytics/greeks: HTTP 429 with base64-encoded error body | high (SDK source) |

### Critical notes for BruteX / TickVault

1. **BruteX lake fit (historical):** default 1-min depth = 6 months —
   far short of BruteX's Groww 2020-01-01 1-minute floor (CLAUDE.md §6).
   "Extended History" add-on depth and price are UNVERIFIED — THE key
   sales/probe question for any BruteX backfill use. Expired-contract
   history: UNVERIFIED. Corporate-action adjustment policy: UNVERIFIED.
2. **TickVault fit (live):** genuine tick-stream (with tick sequence
   numbers, bid/ask, OI), 1-min/5-min server-side bars, full-market feed
   option, BSE L2 depth, and a free **replay service** for off-hours
   development — the richest live surface in this two-vendor study.
   Exact latency SLA: UNVERIFIED.
3. **Three auth patterns in one vendor** (bearer / WS query-string /
   plain query params) — an adapter must implement all three; credentials
   travel in URLs on two of them (secret-handling caution).
4. **INDIA VIX symbol: UNVERIFIED** (NSE indices covered as a segment;
   the spot-index symbol-list file was not retrievable). Probe
   `getAllSymbols` live.
5. **ENGINE UNCHANGED** — this pack is data-source research only; the
   BruteX engine stays NIFTY/BANKNIFTY/SENSEX regardless of vendor
   (CLAUDE.md §2 Rule 3).

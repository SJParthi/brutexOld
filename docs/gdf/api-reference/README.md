---
title: GDF (Global Datafeeds) APIs — Local Documentation Reference
researched_at: 2026-07-15
vendor: Global Datafeeds ("GFDL", Global Financial Datafeeds LLP) — globaldatafeeds.in
purpose: Complete offline reference of Global Datafeeds' public API documentation surface, for BruteX + TickVault vendor evaluation (DEC-GDF-TRUEDATA-DOCS-001, MIGRATION.md #1795)
authority: RESEARCH-GRADE reference (relay/package-evidenced), NOT probe-verified. Unlike docs/groww/api-reference/ (the live vendor's authoritative pack), NOTHING in this directory may be encoded as fact in code without a credentialed live probe + a new DEC. Confidence stamps per claim are binding.
---

# GDF (Global Datafeeds) APIs — Local Documentation Reference

Complete research capture of **Global Datafeeds'** public API documentation
(globaldatafeeds.in — Indian market-data vendor; NSE / BSE / MCX / CDS
real-time + historical data), researched **2026-07-15** for BruteX and
TickVault. "GDF" and "GFDL" both refer to this vendor (legal entity
Global Financial Datafeeds LLP — medium confidence, see
[10-company-support-products.md](./10-company-support-products.md)).

## Retrieval honesty (Golden Rule #1/#10 — read before citing anything)

This build environment's egress proxy **CONNECT-403-blocked every vendor
host** (`globaldatafeeds.in`, `*.lisuns.com`) and every archive/relay
fallback (`web.archive.org` etc.) — a 403 here is an ENVIRONMENT block,
not vendor truth. Evidence channels actually used, and the per-claim
confidence legend used across ALL files in this directory:

| Method | Meaning | Confidence cap |
|---|---|---|
| `pypi-fetch` | **Vendor-authored PyPI packages** fetched directly (author "Global Datafeeds \<developer@globaldatafeeds.in\>": `GFDLWS` 0.0.4, `ws-gfdl` 1.1.0, `gfdl-rest` 1.0.3 — READMEs + full source read here; the COMPLETE family is 7 packages per the sibling census below). Vendor-published code = vendor truth for the wire protocol. | **high** |
| `search-relay` | WebSearch server-side retrieval of vendor doc pages (content relayed, never byte-read here) | **medium** (high only when triangulated with a vendor-authored artifact) |
| `github-fetch` | Third-party repos on GitHub (working integrations) | **low** for vendor truth (medium when ≥2 independent repos agree + vendor corroboration) |
| — | Could not retrieve at all | **UNVERIFIED** |

Every claims table in this pack carries Source + access date + confidence.
A credentialed live probe is MANDATORY before any figure here is encoded
in BruteX/TickVault code. Full URL-by-URL attempt log:
[11-retrieval-log.md](./11-retrieval-log.md).

## Licensing posture — SUMMARIZE-ONLY (DEC-GDFL-VENDOR-DOCS-001, MIGRATION.md #1780)

GDF's subscriber terms include personal-use clauses and its redistribution
terms are UNVERIFIED, so the standing coordinator ruling for GDF material
in this PUBLIC repo is **summarize-only**: no verbatim vendor prose and no
vendor response-sample dumps are committed. API **facts and identifiers**
— endpoint/function/MessageType names, parameter names, enum tokens,
field names, JSON key shapes, identifier grammars — are facts and are
recorded (request key shapes carry placeholder values; response samples
are recast as field-inventory tables; quoted vendor sentences are
paraphrased with attribution). Verbatim originals stay re-fetchable
bit-for-bit via the pinned PyPI digests in
[docs/vendors/gdfl/sdk/PACKAGES.md](../../vendors/gdfl/sdk/PACKAGES.md).
This pack was conformed to the ruling on 2026-07-15 (MIGRATION.md #1795
reconcile note).

### Sibling archive — docs/vendors/gdfl/ (read both)

A parallel GDFL capture (2026-07-13, DEC-GDFL-VENDOR-DOCS-001) landed on
main while this pack was in flight. The two are complementary; this pack
cross-references it ONE-WAY (its files are not edited here):

| Question | Authority |
|---|---|
| Finest-granularity + depth VERDICT (incl. raw-sample measurements + 10 credentialed probe gates) | [docs/vendors/gdfl/VERDICT.md](../../vendors/gdfl/VERDICT.md) |
| PyPI SDK census (7 packages, defects incl. the broken `getbyperiod`, pinned SHA256 re-fetch digests) | [docs/vendors/gdfl/sdk/PACKAGES.md](../../vendors/gdfl/sdk/PACKAGES.md) |
| CURRENT-package WS MessageType inventory (wsgfdl-py 1.3.5 — 38 requests / 36 results) | [docs/vendors/gdfl/api/WEBSOCKET_API.md](../../vendors/gdfl/api/WEBSOCKET_API.md) |
| REST surface own-words summary (incl. gfdl-rest defect list) | [docs/vendors/gdfl/api/REST_API.md](../../vendors/gdfl/api/REST_API.md) |
| Vendor doc-portal section-by-section walkthrough + endpoints/limits/pricing/symbology/company + the TrueData comparison | THIS pack (+ [docs/truedata/api-reference/](../../truedata/api-reference/README.md) + [docs/research/GDF_TRUEDATA_FEASIBILITY.md](../../research/GDF_TRUEDATA_FEASIBILITY.md)) |

## File index (mapped to the vendor's own doc tree)

The vendor's docs portal lives under
`globaldatafeeds.in/global-datafeeds-apis/…` with top-level sections:
Introduction · WebSockets API · REST API · Advanced Streaming API ·
Greeks API · COM API · DotNet API · Code Samples · FAQs · Pricing & Sales ·
Contact (+ site-level Symbol Naming Conventions and knowledge-base
articles). This pack covers every section:

| File | Covers (vendor section) |
|---|---|
| [01-introduction.md](./01-introduction.md) | Introduction (intro / type of APIs / type of data available), doc-portal map, company scope claims |
| [02-connect-auth.md](./02-connect-auth.md) | How to connect (WebSockets + REST), authentication, endpoints & ports, output formats, security posture |
| [03-websocket-functions.md](./03-websocket-functions.md) | WebSockets API — full function vocabulary, streaming subscriptions, on-demand quotes, message-type index, common parameter table |
| [04-historical-data.md](./04-historical-data.md) | GetHistory / GetHistoryAfterMarket / GetHistoryGreeks (WS + REST), periodicities, timestamp semantics, historical DEPTH, IEOD packs, caps |
| [05-instruments-reference.md](./05-instruments-reference.md) | Instrument master & discovery functions, exchange snapshot, market/exchange messages, server info, top gainers/losers |
| [06-greeks-optionchain.md](./06-greeks-optionchain.md) | Greeks API + Advanced Streaming API (option chains, greeks — snapshot, stream, history) |
| [07-symbology.md](./07-symbology.md) | Symbol Naming Conventions — exchange tokens, identifier grammars, continuous futures, short ids, tokens |
| [08-limits-entitlements-pricing.md](./08-limits-entitlements-pricing.md) | FAQs (limits) + GetLimitation entitlement model + Pricing & Sales + trial |
| [09-sdks-code-samples.md](./09-sdks-code-samples.md) | Code Samples + COM/DotNet API downloads + vendor PyPI packages + third-party integrations |
| [10-company-support-products.md](./10-company-support-products.md) | Contact/support, company & exchange authorization, desktop products (NimbleDataPro family), ops |
| [11-retrieval-log.md](./11-retrieval-log.md) | Every URL attempted → outcome (the honesty backbone) |

## Quick reference card

### Protocol in one paragraph (high confidence — vendor packages)

ONE JSON function vocabulary served over two transports: a **WebSocket**
(`{"MessageType":"<Function>", …}` request → `…Result` response; streaming
via `Subscribe*` MessageTypes) and a **REST mirror** of the same functions
(`GET {base}/{Function}/?accessKey=…&param=…`, default JSON, opt-in
`xml=true` or `format=CSV`). Auth = ONE API key: WS sends
`{"MessageType":"Authenticate","Password":"<API key>"}` once per
connection; REST passes `accessKey=` on every call.

### Endpoints (per-customer provisioned — the docs deliberately do not print them)

| Surface | Observed value | Confidence |
|---|---|---|
| WebSocket | `ws://nimblewebstream.lisuns.com:4575` | medium (2 independent third-party repos + vendor-FAQ telnet test of port 4575) |
| REST | `http://nimblerest.lisuns.com:4531` / TLS `https://…:4532` | medium (≥4 third-party repos); TLS port medium-low (seen once) |
| TLS WebSocket (`wss://`) | UNVERIFIED (zero evidence) | UNVERIFIED |

Per the vendor reference, the endpoint URL and port are issued to each
subscriber by Global Datafeeds (paraphrased; GFDLWS README, pypi-fetch,
high).

### The 29-function vocabulary (+5 newer)

Realtime: SubscribeRealtime · SubscribeSnapshot; Quotes: GetLastQuote
(+Short/+ShortWithClose/+Array×3) · GetSnapshot · GetExchangeSnapshot;
History: GetHistory · GetHistoryAfterMarket; Reference: GetExchanges ·
GetInstruments(OnSearch) · GetInstrumentTypes · GetProducts ·
GetExpiryDates · GetOptionTypes · GetStrikePrices; Account/ops:
GetServerInfo · GetLimitation · GetMarketMessages · GetExchangeMessages;
Options/Greeks: GetLastQuoteOptionChain · SubscribeRealtimeGreeks ·
GetLastQuoteOptionGreeks(+Array/+Chain); newer additions:
SubscribeOptionChain · SubscribeOptionChainGreeks ·
SubscribeTopGainersLosers · GetTopGainersLosers · GetHistoryGreeks.
(Vendor GFDLWS 0.0.4 README + package modules, pypi-fetch, high.)
The CURRENT `wsgfdl-py` 1.3.5 widens this to 38 request / 36 result
MessageTypes (adds GetVolumeShockers, SubscribeSnapshotGreeks,
GetSnapshotGreeks, an AdjustSplits history flag) — full inventory in
[docs/vendors/gdfl/api/WEBSOCKET_API.md](../../vendors/gdfl/api/WEBSOCKET_API.md).

### Load-bearing numbers (with confidence)

| Fact | Value | Confidence |
|---|---|---|
| Realtime update frequency | 1-second snapshots (not raw exchange ticks) | high (vendor README) / medium (tick-semantics inference) |
| Bundled history depth | tick 7 days · 1–4 min 3 months · 5–10 min 4.5 months · 15–30 min + hourly 6 months · D/W/M since 2010 | medium (vendor "Type of Data Available" page, search-relay) |
| Deep 1-min history | separate purchased IEOD file packs (NSE F&O since 2010-09-01) — NOT the API | low (third-party) |
| Array-call cap | 25 instruments per array call; search returns max 20 | high (vendor READMEs) |
| Request quotas | per-hour, plan-based (FAQ examples 1800 / 3600 / 7200 req/h) | medium (FAQ relay) |
| Per-second rate cap | UNVERIFIED (model is per-hour + streaming) | UNVERIFIED |
| WS request timestamps | epoch SECONDS; REST responses epoch **MILLISECONDS** with ALL-CAPS field names | high (vendor packages) |

### Critical notes for BruteX / TickVault

1. **BruteX lake fit (historical):** API-bundled 1-minute depth (~3
   months) is FAR shallower than BruteX's probe-verified Groww
   2020-01-01 1-minute floor (CLAUDE.md §6). Deep 1-min exists only as
   purchased IEOD FILES (not API pulls) at low confidence. Whether deep
   1-min can be enabled per-key on the API is UNVERIFIED — a sales/probe
   question, and THE blocking question for any BruteX backfill use.
2. **TickVault fit (live):** the feed is 1-second snapshot-based with
   L1-only book data (no L2 depth evidenced); "tick" periodicity rows are
   second-stamped. GetExchangeSnapshot offers whole-exchange 1-min bars in
   one call — relevant to whole-universe capture.
3. **Two timestamp conventions** (WS seconds vs REST milliseconds +
   ALL-CAPS) — any adapter must normalise at the parse boundary.
4. **Everything is per-key entitlements** (GetLimitation): exchanges,
   functions, intervals, record caps, quotas — two accounts can see
   entirely different capability surfaces. Always probe `GetLimitation`
   first.
5. **No India VIX identifier evidenced**; NSE_IDX segment exists but the
   exact VIX symbol is UNVERIFIED — probe `GetInstruments('NSE_IDX')`.
6. **ENGINE UNCHANGED** — this pack is data-source research only; the
   BruteX engine stays NIFTY/BANKNIFTY/SENSEX regardless of vendor
   (CLAUDE.md §2 Rule 3).

# 09 — Ops, data quality, market calendar, support, products, changelogs

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Vendor identity & authorization

| Claim | Value | Source | Confidence |
|---|---|---|---|
| Company | TrueData Financial Information Pvt Ltd | Medium handle "@truedatapvtltd" (search-relay); PyPI author emails @truedata.in (direct-fetch) | high |
| Exchange authorization | "Authorized Realtime (L1) Data Vendor of NSE (Stocks, Indices, F&O & Currency), BSE (Stocks, Indices & F&O) and MCX" | truedata.in homepage/why snippets via search-relay | medium |
| Product suite | Velocity (desktop RT+historical plugin), Market Data API, Options Decoder, Cloud VPS, NinjaTrader Indicators | /products/all-products via search-relay | medium |

## 2. Bars, timestamps & data-quality statements

- **Bar formation** (KB "How are the bars formed?", search-relay,
  medium): "For data coming from: 09:15:00 to 09:15:59 > bar 09:15;
  09:16:00 to 09:16:59 > bar 09:16" — bars are labeled by the
  bucket-OPEN minute, formed from that minute's ticks. (Matches BruteX's
  own resample-labeling convention — a genuine compatibility point.)
- **"Is the data delayed?"** — the vendor's answer was NOT retrieved (the
  article is 403-blocked; the relay snippet covered only bar formation).
  Marketing says "low latency" and "L1 data at 1-second frequency with
  single best bid/ask" (product pages via search-relay, medium). Exact
  latency SLA: **UNVERIFIED**.
- **Live vs touchline**: a forum topic exists ("Difference in Live Data
  vs touchline data", feedback.truedata.in/topic/32241) — content
  unretrieved (403).
- **Corporate actions**: REST `/getcorpaction?symbol=` exists on the
  history host (pkg-source, high); .NET `GetCorporateActions()` (NuGet,
  direct-fetch). Whether HISTORICAL BARS are corporate-action ADJUSTED:
  **UNVERIFIED** — no adjustment-policy statement was retrieved. (For the
  BruteX immutable keep-first lake this is a mandatory probe item — see
  the mutable-series risk discussion in
  `docs/research/ZERODHA_HISTORICAL_FEASIBILITY.md` §5.)
- **Symbol renames tracked**: `/getsymbolchangehistory` (pkg-source, high).
- **Bhavcopy availability check**: `/getbhavcopystatus`; SDK error text
  "No complete bhavcopy found for requested date. Last available for
  <timestamp>" (pkg-source, high).

## 3. Market hours / calendar handling

- NO machine-readable holiday-calendar endpoint exists in any SDK
  (source grep for 'holiday' — none). Market status is runtime-only: the
  WS `getmarketstatus` command + `marketstatus` events (pkg-source,
  high). Holiday-calendar API: UNVERIFIED/absent — an adapter would keep
  using BruteX's own `brutex/calendars/nse.py` as the calendar authority.
- The replay feed covers non-market-hours development (daily post-market
  + weekends), free for API subscribers (KB via search-relay, medium).
- The Node full-feed SDK refreshes the symbol master daily ~02:40 UTC
  (~08:10 IST) before market open (pkg-source, high — client-side).

## 4. Status/announcement channels & support

- Telegram: `t.me/truedata_ws_api` (Market Data API — linked from BOTH
  PyPI packages + the npm README; direct-fetch, high);
  `t.me/s/truedata_velocity` (Velocity; search-relay, medium). Content
  unretrieved (t.me blocked).
- Support portal: feedback.truedata.in — knowledge base + FAQs + forum +
  ticketing + ideas board (search-relay, high for existence).
- No public status page discovered (status.truedata.in etc.) —
  UNVERIFIED whether one exists.
- SDK support emails observed: nahas@truedata.in (Python author),
  kapil@truedata.in (older SDK author), support@truedata.in (npm
  maintainer) — direct-fetch, high.

## 5. Sandbox / developer experience

- `wstest.truedata.in` — "Websocket/REST JS Client": a browser test
  client for real-time + historical + EOD using your API credentials
  (KB + search-relay titles, medium).
- KB articles exist for fetching real-time data and EOD data on the
  sandbox page (titles only; 403-blocked).
- Vendor sample-code repo (github-fetch, high):
  github.com/Nahas-N/Truedata-sample-code — analytics, callback,
  full-feed, history, option-chain samples.

## 6. TrueData Options Decoder (separate product)

An options-analytics END-USER tool (not an API): Option Chain Replay, OI
graphs; its own KB category + getting-started articles (search-relay,
medium). API-level exposure: UNVERIFIED.

## 7. Changelog / migration history (registry pages, direct-fetch, high)

| Line | History |
|---|---|
| Python `truedata` | v7.0.1 (2025-07-14) compression bugfix · v7.0.0 WS compression · v6.0.5 analytics compression · v6.0.0 module split + DataFrame returns |
| Python `truedata-ws` | 62 releases; **historical-WS → REST migration at v3.0.1** (the service itself retired ~2021-04-30); last release 5.0.11 (2023-11-16) |
| Node `truedata-nodejs` | 1.0.x (2021–2024) → 1.1.0 (2026-01-14) → 1.2.0 (2026-05-08) — the corporate-APIs + option-chain-events era |
| .NET `TrueData-DotNet` | 1.4.7 (2025-07-29); several older versions deprecated "due to critical bugs" |
| Velocity 2.0 | has its own KB change-log article (403-blocked) |
| Official PDF | v2.2.1 observed in the wild (Scribd mirror, 2023-era; unretrieved) |

## 8. BruteX/TickVault-relevant gap notes (honest, consolidated)

1. **1-minute historical depth is only 6 months by default** (vs
   BruteX's 2020→today Groww floor); the Extended-History add-on's depth
   and price are UNVERIFIED — THE key sales question.
2. Expired options / expired contract-level history over REST:
   UNVERIFIED (continuous `-I/-II/-III` exist; per-contract expired
   queries not evidenced).
3. Corporate-action adjustment policy for historical bars: UNVERIFIED.
4. INDIA VIX symbol availability: plausible, UNVERIFIED.
5. No documented daily quota beyond the per-hour caps; exact latency SLA
   unverified; no holiday-calendar API.

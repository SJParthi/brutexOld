# TrueData live-feed granularity + depth — the definitive verdict

> **Compiled:** 2026-07-20. Evidence tiers per `README.md`. Nothing below is copied
> from vendor prose beyond attributed ≤25-word quotes; identifiers (`trade`,
> `bidaskL2`, field names, hosts/ports) are vendor API tokens kept verbatim by
> necessity. The GDFL twin of this file is `../gdfl/VERDICT.md`; the cross-vendor
> decision lives in `../GDFL_VS_TRUEDATA_VERDICT.md`.

## Verdict (headline)

**TrueData's live WebSocket "tick" stream is a ~1-second-class COALESCED L1 snapshot
feed — second-stamped, netted (LTQ/day-cumulative fields, not per-order events),
carrying one best bid + one best ask — NOT NSE order-by-order tick-by-tick (TBT).
The marketing phrase "tick-by-tick" means "every ~1-second-class snapshot update",
which is the ceiling NSE's vendor distribution imposes on ALL retail vendors. Depth
is L1-only on NSE and MCX; the ONE depth exception is BSE, where a 5-level `bidaskL2`
stream exists. History depth is shallow (tick ≈ 5 trading days, 1-min ≈ 6 months
bundled), so a 2020→today backfill is NOT available from TrueData as bundled —
forward capture is the only long-series path, exactly as with GDFL.**

Terminology note (mirrors `../gdfl/VERDICT.md`): we call this data **"~1-second-class
L1 snapshot data"**. Each `trade` message is a Level-1 quote/trade snapshot (LTP, LTQ,
day-cumulative ATP/TTQ/turnover, day OHL, prev close, OI, prev-OI, special tag,
per-symbol `tick_seq`, best bid/ask) stamped at second resolution. The vendor calls
each such message a "tick"; where "tick" appears below it means exactly this snapshot
class, never an exchange order event.

## The three strongest evidence rows (the anchor of the verdict)

1. **The vendor's own product feature grid:** "Streaming Live Data API — L1 data
   @1sec frequency with single best bid/ask details" — the only explicit frequency
   number in the whole corpus. Tier **OFFICIAL-VERIFIED(capture-2026-07-15)**
   (truedata.in/products/marketdataapi via
   `docs/broker-api-capture/truedata/01-overview.md`).
2. **The SDK wire format cannot even express sub-second data:** every data-message
   timestamp in the official `truedata` 7.0.1 SDK parses `'%Y-%m-%dT%H:%M:%S'` —
   second resolution, no sub-second field exists anywhere — and the LZ4-compressed
   binary protocol carries timestamps as 4-byte epoch SECONDS. A per-trade,
   sub-second feed is unrepresentable on this wire. Tier **SDK-VERIFIED**
   (`truedata/websocket/utils.py` dataclasses + `compression_map.py`; wheel
   sha256-pinned in `MANIFEST.md`).
3. **NSE itself does not let ANY vendor rebroadcast TBT:** "Tick-by-tick data is
   available only at NSE co-lo servers. The TBT data is not available at TAP Server
   or through DotEx for further broadcast." Tier **OFFICIAL-SNIPPET-UNCONFIRMED**
   (nseindia.com/static/market-data/real-time-data-subscription — page blocked;
   snippet). TrueData's own blog concedes the distinction: NSE TBT is "every order
   or a change in the order" — a product class it does not sell
   (OFFICIAL-SNIPPET-UNCONFIRMED, truedata.in/blog/levels-real-time-data-nse-bse-mcx).

## Why ~1-second is the ceiling, not a TrueData compromise

Prior verified team research (tick-feed research 2026-07-09; restated in
`../gdfl/VERDICT.md`): NSE distributes a ~1-second conflated snapshot broadcast to
its authorized vendors; genuine sub-second order-by-order data (TBT/MTBT) reaches
trading members at colocation via multicast. Tier **PRIOR-VERIFIED-NOTE**. The
2026-07-20 NSE-side research adds nuance, all OFFICIAL-SNIPPET-UNCONFIRMED (NSE PDFs
blocked): NSE's domestic info-vending tariff DOES price a vendor-side "Tick by Tick"
tier for the Capital Market segment (~₹50 lakh/yr fixed + per-user fees — figures
from a search summary of the 2025 tariff PDF, unread), which sits in tension with the
"not through DotEx" snippet above (same page family, different snippet passes — a
third snippet of the SAME page reads: TBT "delivered multicast over a dedicated
leased line from an NSE PoP… Alternatively, the customer can take the data from one
of our authorized data vendors" (OFFICIAL-SNIPPET-UNCONFIRMED) — all three stay
unconfirmed and the tension stays OPEN until the pages/PDFs are read first-hand).
Even on the permissive
reading, NO evidence anywhere shows TrueData (or GDFL) holding or reselling that
tier; TrueData self-describes as an "authorized Realtime (L1) Data Vendor"
(OFFICIAL-SNIPPET-UNCONFIRMED, about-us), and its own blog states NSE stopped
providing F&O TBT to vendors ~2020 (COMMUNITY-tier snippet). Net: for the segments
BruteX/TickVault care about, ~1-second-class L1 snapshots are the ceiling for every
retail vendor feed, TrueData included.

## The facts

### 1. What the live socket delivers (SDK-VERIFIED unless noted)

- `wss://push.truedata.in:<port>?user=<USER>&password=<PASSWORD>` — TLS WebSocket,
  credentials in the query string; port is ACCOUNT-ASSIGNED (8082 legacy default,
  8084 current SDK default). Full surface in `api/WEBSOCKET_API.md`.
- Message families: `touchline` (18-field snapshot on subscribe) · `trade` (19-field
  tick incl. `tick_seq` + Special tag) · `bidask` (L1) · `bidaskL2` (BSE-only, 5
  levels) · `bar1min`/`bar5min` (server-built completed bars) · `greeks` (8-field
  add-on stream) · market status · heartbeat (~5 s).
- The `trade` record is a NETTED snapshot: day-cumulative TTQ/ATP/turnover + day
  O/H/L + OI ride on every message — the shape of a conflated quote image, not an
  exchange trade print. `tick_seq` proves lossless delivery OF WHAT IS SENT, not
  every-trade delivery (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog).
- Emission model: event-on-change reading (SDK event wording + `tick_seq`), so >1
  msg/s per symbol is possible on busy symbols — but per-segment measured msgs/sec
  is UNVERIFIED (probe gate 1). The "@1sec" grid line is the vendor's own number.

### 2. Depth

- **NSE + MCX: L1 ONLY** — "For Nse symbols we offer level 1 bid ask which have one
  best bid and ask data" (SDK-VERIFIED, official `truedata` README/METADATA — an
  OSS-licensed text; corroborated OFFICIAL-VERIFIED(capture-2026-07-15) KB).
- **BSE (incl. BSE F&O): L2 = top-5** bid + ask, per level (price, qty, order-count),
  plus totals — the `bidaskL2` 35-element message (SDK-VERIFIED); product page:
  "Market Depth API (Exclusive to BSE) — Top 5 bid and ask levels"
  (OFFICIAL-VERIFIED(capture-2026-07-15)).
- **No 20-level product, no order-book/order-level product anywhere** in SDKs,
  README, product pages, or KB (absence across SDK-VERIFIED +
  OFFICIAL-VERIFIED(capture) surfaces — absence of evidence, not a vendor denial).

### 3. Authorization + entity

- Entity: **TrueData Financial Information Pvt. Ltd.** (Ahmedabad corporate office;
  Panchkula registered office) — OFFICIAL-VERIFIED(capture-2026-07-15).
- Claim: "Authorised Data Vendor for NSE, BSE and MCX"; about-us wording "authorized
  Realtime (L1) Data Vendor" for NSE (Stocks, Indices, F&O, Currency), BSE, MCX —
  OFFICIAL-VERIFIED(capture) + OFFICIAL-SNIPPET-UNCONFIRMED (about-us blocked today).
- NSE-side: "TrueData Financial Information Private Limited" appears in NSE's
  "AUTHORISED REALTIME DATA VENDORS" PDFs per search summaries —
  **OFFICIAL-SNIPPET-UNCONFIRMED**: no NSE PDF was fetchable (proxy 403); the entity
  row was never eyeballed. One off-proxy download closes this (MANIFEST upload list).
- The authorization TIER is L1 by the vendor's own self-description; the NSE list's
  columns appear to be SEGMENTS (CM/F&O/CD), not data levels
  (OFFICIAL-SNIPPET-UNCONFIRMED) — so even the PDF likely cannot prove tier.

### 4. Latency claims — honesty

- Marketing: "Ultra-low latency (milliseconds)", "99.995% uptime", redundant DCs —
  OFFICIAL-VERIFIED(capture-2026-07-15) product pages.
- **No numeric latency SLA (p50/p99, ms figure) exists anywhere found** — UNVERIFIED
  beyond the adjective. Physical floor: the feed itself is ~1-second-class, so
  "milliseconds" can only describe DELIVERY delay of each snapshot, never sub-second
  data granularity.

### 5. History depth (the make-or-break; detail in `api/HISTORY_API.md`)

- Bundled defaults: **tick ≈ last 5 trading days · 1-min bars ≈ last 6 months ·
  EOD 10+ years** — OFFICIAL-SNIPPET-UNCONFIRMED (KB blocked today) but identical in
  the 2026-07-15 capture + the #1966 pack (npm README pins the tick-5-days line at
  its high tier: "Max upto last 5 days of tick data"). "Extended History" is a PAID
  ADD-ON whose depth and price are **UNVERIFIED** — the #1 sales question.
- Verdict: a 2020→today 1-minute (or any tick-class) BACKFILL is NOT available from
  TrueData as bundled — an order of magnitude short of the BruteX lake floor
  (`docs/research/GDF_TRUEDATA_FEASIBILITY.md` Q1: NO). Forward capture from
  subscription day is the only long-series path. Groww (1-min to 2020) stays
  strictly superior for the lake.

## Evidence table

| # | Claim | Verdict | Tier | Source |
|---|---|---|---|---|
| 1 | Live product = "L1 data @1sec frequency" with single best bid/ask | CONFIRMED (vendor's own grid) | OFFICIAL-VERIFIED(capture-2026-07-15) | truedata.in/products/marketdataapi via broker-api-capture 01-overview |
| 2 | SDK wire timestamps are second-granular everywhere; compressed protocol = 4-byte epoch seconds; no sub-second field exists | CONFIRMED | SDK-VERIFIED | truedata 7.0.1 `utils.py` + `compression_map.py` (sha256-pinned) |
| 3 | `trade` record is a netted L1 snapshot (day-cumulative fields + OI on every message) with per-symbol `tick_seq` | CONFIRMED | SDK-VERIFIED | `TD_ws.py:141-162`, `constants.py` tick_fields |
| 4 | NSE TBT = order-by-order, colo/members only; not rebroadcast via DotEx | SUPPORTED, unconfirmed | OFFICIAL-SNIPPET-UNCONFIRMED | nseindia.com real-time-data-subscription (blocked; snippet) |
| 5 | A vendor-side TBT tariff tier exists on paper (CM, ~₹50L/yr fixed) — no evidence any retail vendor exercises it | SUPPORTED, unconfirmed (tension with row 4 noted) | OFFICIAL-SNIPPET-UNCONFIRMED | Real_Time_Tariff_Domestic_01042025_.pdf (blocked; search summary) |
| 6 | TrueData's own blog distinguishes retail 1-second snapshots from NSE TBT ("every order or a change in the order"); F&O vendor TBT stopped ~2020 | SUPPORTED | OFFICIAL-SNIPPET-UNCONFIRMED / COMMUNITY | truedata.in blog levels-real-time-data (blocked; two snippet relays) |
| 7 | Marketing "tick-by-tick" / "Full-Precision Ticks — every price movement captured" contradicted by the vendor's own technical surfaces | CONFIRMED (the honesty split) | OFFICIAL-VERIFIED(capture) marketing vs rows 1–3 | market-data-apis page via capture 01-overview |
| 8 | NSE + MCX depth = L1 only | CONFIRMED | SDK-VERIFIED + OFFICIAL-VERIFIED(capture) | truedata METADATA (OSS README); getting-started KB |
| 9 | BSE depth = L2 top-5 (`bidaskL2`, 35 elements, order-counts + totals) | CONFIRMED | SDK-VERIFIED + OFFICIAL-VERIFIED(capture) | compression map "D"; truedata-ws changelog; product page |
| 10 | Heartbeat ~5 s server-side; client pings 10 s; auto-reconnect re-subscribes | CONFIRMED | SDK-VERIFIED (+capture) | `TD_ws.py:25`, `TD_live.py:71` |
| 11 | 1 concurrent WS login per user; force-logout endpoint exists | CONFIRMED | OFFICIAL-VERIFIED(capture-2026-07-15) | KB request/call-limit article via capture |
| 12 | Plan envelope announced at connect (`maxsymbols`/`subscription`/`validity`) | CONFIRMED | SDK-VERIFIED | `TD_ws.py:96-99` + compression map "A" |
| 13 | History depth: tick ~5 trading days · 1-min ~6 months · EOD 10+ yr; Extended History = paid add-on, depth/price unknown | SUPPORTED, unconfirmed (5-day tick line SDK-corroborated) | OFFICIAL-SNIPPET-UNCONFIRMED + PRIOR-VERIFIED-NOTE (#1966: npm README high) | feedback.truedata.in KB (blocked); npm truedata-nodejs README |
| 14 | Entity on NSE's authorized-vendor list | SUPPORTED, unconfirmed | OFFICIAL-SNIPPET-UNCONFIRMED | NSE PDFs (blocked; search summaries only) |
| 15 | API pricing custom-quoted; FYERS partner figures ₹1,599/₹2,599+GST/mo, 200 symbols default; trial ≤10 days | Pricing model CONFIRMED (capture); figures COMMUNITY-only | OFFICIAL-VERIFIED(capture) + COMMUNITY | capture 01-overview; fyers.in notice-board (blocked; snippets) |

## Implications for BruteX / TickVault

- **Live:** TrueData does not pierce the retail ~1-second L1 ceiling — no vendor
  does. Its value is the ENGINEERING surface around the same data class: wss TLS,
  5 s heartbeat + client reconnect law, LZ4, `tick_seq` gap detection, server bars,
  replay service, sandbox, BSE L2 (see `../GDFL_VS_TRUEDATA_VERDICT.md` §3). Any
  capture leg stays OUT of BruteX per DEC-LIVE-REMOVE-001 and needs its own DEC.
- **Historical:** unfit for the BruteX 2020 lake floor as bundled (row 13); the
  Extended-History add-on is the only conceivable path and is unpriced/undepth'd.

## Credentialed probe gates (pre-adoption; each needs a trial key)

Every one is OPEN until a live probe transcript lands (trial ≤10 days + sandbox
`wstest.truedata.in` make these cheap):

1. **Measured cadence per segment** — count msgs/sec on a liquid NIFTY future +
   option + `NIFTY 50` + `INDIA VIX` during open; do distinct trades appear between
   snapshots; inspect `tick_seq` gaps.
2. **Heartbeat-vs-change emission** — does a quiet symbol emit every second anyway,
   or skip seconds (the GDFL 23a-transcript question, TrueData edition).
3. **Timestamp precision on the wire** — any sub-second digits (expected NO, per
   evidence row 2).
4. **Raw JSON keys** — capture uncompressed + LZ4 frames (or obtain the gated API
   PDF v2.2.1); the SDK array-order reconstruction is not wire truth.
5. **Extended-History depth + price** — the decisive lake question (sales).
6. **INDIA VIX** symbol servability on the live feed + history.
7. **Port→plan map** (8082/8084/9084/9092 assignment law) + numeric symbol tiers
   beyond "200 default / 50 trial".
8. **Latency measured** (delivery delay distribution vs exchange time) — no vendor
   SLA exists to test against; measure absolute.
9. **Expired-contract history** servability (UNVERIFIED both vendors — #1966 Q3).
10. **NSE authorized-vendor PDF row** eyeballed first-hand (an off-proxy download,
    not a probe — MANIFEST upload list item 1).

## Honest limits (what remains unverified, and what would verify each)

- **Per-trade-vs-conflated cadence was never measured** — the verdict rests on the
  vendor's own "@1sec" line + the second-granular wire + the NSE ceiling; a live
  capture (gate 1) is the falsifier. If a probe showed sustained multi-Hz per-symbol
  updates with sub-second content, the CLASS verdict would need re-examination (the
  wire format makes sub-second timestamps impossible regardless).
- **All KB-sourced numbers** (5d/6mo/10+yr depth; REST rate caps; 1-login) are
  relay/capture-tier — never byte-read this session; silently driftable.
- **NSE-side documents** (vendor list, TBT tariff, feed specs) were ALL blocked —
  every NSE fact here is snippet-tier, including the ₹50L TBT-tier figure and the
  row-4/row-5 tension, which only reading the pages can resolve.
- **Pricing figures** are partner/reseller COMMUNITY tier, of unknown date.
- Ports/entitlements are account-shaped; nothing here binds what a purchased key
  actually gets (`maxsymbols`, streams, segments) — the connect-time announce is the
  per-account oracle.
- Everything sourced from SDKs may LAG the live docs (site unreachable); the
  operator upload list in `MANIFEST.md` exists to close that.

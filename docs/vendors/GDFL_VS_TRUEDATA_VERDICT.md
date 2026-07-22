# GDFL vs TrueData — the live-feed decision verdict

> **Compiled:** 2026-07-20. The decision-grade side-by-side for the operator's
> question: *which of GDFL vs TrueData provides a live WebSocket feed with GENUINE
> tick-by-tick data directly from NSE — and is that even real for retail?*
>
> **Authorities this doc composes (cite these for detail, don't re-derive):**
> `docs/vendors/gdfl/VERDICT.md` (the GDFL granularity/depth authority,
> DEC-GDFL-VENDOR-DOCS-001 #1780) · `docs/vendors/truedata/VERDICT.md` (the TrueData
> live-socket authority, 2026-07-20) · `docs/truedata/api-reference/` +
> `docs/gdf/api-reference/` + `docs/research/GDF_TRUEDATA_FEASIBILITY.md` (the
> DEC-GDF-TRUEDATA-DOCS-001 #1966 protocol packs, 2026-07-15) ·
> `docs/broker-api-capture/truedata/` (the 2026-07-15 official-page capture).
> Evidence tiers per `truedata/README.md`; every cell below carries its tier.
> Conflicts resolve strongest-tier-first, newest-date-second, stated in-place.

## §1 The question, answered (TL;DR verdict box)

> **(a) Genuine sub-second NSE tick-by-tick direct to retail: NOBODY sells it.**
> NSE TBT/MTBT is an order-by-order feed ("every order or a change in the order")
> delivered by multicast to trading members at colocation — auto-provisioned on colo
> racks — and per NSE's own product page "not available at TAP Server or through
> DotEx for further broadcast" (OFFICIAL-SNIPPET-UNCONFIRMED — NSE pages/PDFs all
> proxy-blocked; snippets only). NSE's domestic info-vending tariff DOES price an
> authorized-vendor TBT tier on paper — Capital Market, ~₹50 lakh/yr fixed +
> per-user fees (OFFICIAL-SNIPPET-UNCONFIRMED, 2025 tariff PDF unread; in tension
> with the "not through DotEx" snippet — a third snippet of the SAME page reads:
> TBT "delivered multicast over a dedicated leased line from an NSE PoP…
> Alternatively, the customer can take the data from one of our authorized data
> vendors" (OFFICIAL-SNIPPET-UNCONFIRMED) — the tension stays OPEN, unresolvable
> until the documents are read)
> — but **no evidence anywhere shows EITHER vendor holding or reselling it**, and
> TrueData's own blog states NSE stopped providing F&O TBT to vendors ~2020
> (COMMUNITY). Both GDFL and TrueData are authorized **Realtime (L1)** vendors by
> their own self-description (OFFICIAL-SNIPPET-UNCONFIRMED), and both deliver
> **~1-second-class coalesced L1 snapshot streams** — GDFL vendor-stated "1 second
> frequency" (OFFICIAL-VERIFIED, fresh 2026-07-20 PyPI re-fetch of the vendor WS
> reference); TrueData vendor-stated "@1sec" L1 (OFFICIAL-VERIFIED
> capture-2026-07-15) with an SDK wire format that cannot even express sub-second
> timestamps (SDK-VERIFIED). Marketing "tick-by-tick" on both sites means "every
> ~1-second-class snapshot", not exchange order events.
>
> **(b) The winner for a live L1/depth snapshot socket: TrueData** — on engineering
> surface and operability, NOT on data granularity (equivalent-class on NSE). §3.

### Who actually receives genuine NSE order-by-order TBT

| Receiver class | Reality | Tier |
|---|---|---|
| Trading members at NSE colocation | YES — MTBT multicast, auto-provisioned on colo racks; the only demonstrated live path | OFFICIAL-SNIPPET-UNCONFIRMED (colo guidelines + MTBT spec, PDFs blocked) |
| Authorized data vendors, off-site | ON PAPER ONLY — a priced tariff tier (CM, ~₹50L/yr fixed + per-user fees, leased-line multicast); tension with the "not through DotEx" statement; NO vendor shown exercising it; F&O vendor-TBT reportedly stopped ~2020 | OFFICIAL-SNIPPET-UNCONFIRMED (tariff PDF unread) + COMMUNITY (the F&O-stopped statement, TrueData blog) |
| Retail via GDFL / TrueData / broker APIs | NO — ~1-second-class coalesced snapshot feeds only (L1; L2/L3 MBP at some brokers); the only retail-visible TBT products are member-side DERIVED in-app views | OFFICIAL-VERIFIED (both vendors' own cadence statements) + COMMUNITY (broker calibration) |

### The evidence chain behind §1(a) (the load-bearing rows)

| # | Evidence | Tier |
|---|---|---|
| 1 | TrueData's own product grid: "L1 data @1sec frequency with single best bid/ask details" | OFFICIAL-VERIFIED(capture-2026-07-15) — `docs/broker-api-capture/truedata/01-overview.md` |
| 2 | TrueData SDK wire is second-granular everywhere (timestamps `'%Y-%m-%dT%H:%M:%S'`; compressed protocol 4-byte epoch seconds) — sub-second unrepresentable | SDK-VERIFIED (truedata 7.0.1, sha256-pinned in `truedata/MANIFEST.md`) |
| 3 | NSE: "Tick-by-tick data is available only at NSE co-lo servers. The TBT data is not available at TAP Server or through DotEx for further broadcast." | OFFICIAL-SNIPPET-UNCONFIRMED (nseindia.com real-time-data-subscription, blocked) |
| 4 | GDFL's own WS reference: subscribers "receive the data with 1 second frequency"; RealtimeResult = single best bid/ask, no depth | OFFICIAL-VERIFIED (fresh 2026-07-20 PyPI re-fetch of the vendor-authored reference) |
| 5 | GDFL's own pages pin its "tick by tick" marketing to "tick data at a 1-second interval time stamp" | OFFICIAL-SNIPPET-UNCONFIRMED (blocked pages, 2026-07-20 snippets) |
| 6 | TrueData's own blog: NSE TBT = "every order or a change in the order" — a separate product class it does not sell | OFFICIAL-SNIPPET-UNCONFIRMED (blog blocked; two independent snippet relays) |

Calibration (all COMMUNITY unless noted): the whole retail ecosystem sits at the
same ceiling — Zerodha Kite staff describe their feed as L2 snapshot data at ~1–2
ticks/sec, "not TBT"; Upstox's "TBT Engine" (Oct 2024) exposes member-side
TBT-DERIVED in-app views (charts, 30-level depth), not a raw order-level feed;
NSE IX (GIFT City — a separate NSE-group exchange) officially describes its
retail-relevant realtime feeds as 1-second snapshots (OFFICIAL-SNIPPET-UNCONFIRMED,
family-structure evidence only).

## §2 Per-axis side-by-side

| Axis | GDFL (Global Financial Datafeeds LLP) | TrueData (TrueData Financial Information Pvt Ltd) |
|---|---|---|
| **TBT claim honesty** | Markets "tick by tick", but its own pages pin the meaning: "tick data at a 1-second interval time stamp" (OFFICIAL-SNIPPET-UNCONFIRMED). No order-by-order claim anywhere (UNVERIFIED-negative). The archive verdict: vendor enum `TICK` = 1-second L1 snapshots, "not sub-second exchange tick-by-tick" (PRIOR-VERIFIED-NOTE, gdfl/VERDICT.md) | Markets "tick-by-tick" + "Full-Precision Ticks — every price movement captured" (OFFICIAL-VERIFIED capture-2026-07-15) — contradicted by its OWN product grid ("L1 data @1sec frequency", same tier), its own blog's retail-vs-TBT distinction (OFFICIAL-SNIPPET-UNCONFIRMED), and the second-granular SDK wire (SDK-VERIFIED) |
| **Live cadence** | `SubscribeRealtime` → "receive the data with 1 second frequency" per instrument (OFFICIAL-VERIFIED, 2026-07-20 PyPI re-fetch); full-image 23-field frame each push; whole-second epoch timestamps (SDK-VERIFIED). Emit-on-change vs heartbeat UNMEASURED; branch-doc transcripts suggest indices emit trade-lessly every second while quiet stocks skip seconds (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog of the PR #1595 branch docs) | `trade` stream, vendor grid "@1sec" L1 (OFFICIAL-VERIFIED capture-2026-07-15); netted snapshot records with per-symbol `tick_seq`; second-granular wire, 4-byte epoch-seconds compressed protocol (SDK-VERIFIED). Event-on-change reading ⇒ >1 msg/s plausible on busy symbols; measured msgs/sec UNVERIFIED (probe) |
| **Depth (live)** | **L1 only, every segment** — single best bid/ask; the fresh REST re-fetch confirms NO depth endpoint exists anywhere (OFFICIAL-VERIFIED, 2026-07-20 PyPI); chain `Depth` param = strike-window count, not book depth (PRIOR-VERIFIED-NOTE) | **NSE + MCX: L1 only** (SDK-VERIFIED + capture). **BSE: L2 top-5** with per-level order-counts + totals (`bidaskL2`, SDK-VERIFIED; "Exclusive to BSE" per the product page, capture). No 20-level / order-book product either vendor |
| **NSE authorization** | "Authorised Realtime (L1) Data Vendor" of NSE Data & Analytics (formerly DotEx), + MCX (since 2010), BSE, NCDEX; entity Nashik, est. 2010 (OFFICIAL-SNIPPET-UNCONFIRMED). Appears in NSE's vendor-list PDFs per search summaries only — PDFs unfetchable (OFFICIAL-SNIPPET-UNCONFIRMED) | "Authorized NSE, BSE & MCX Market Data Vendor"; about-us self-description "authorized Realtime (L1) Data Vendor" (OFFICIAL-VERIFIED capture + OFFICIAL-SNIPPET-UNCONFIRMED). Same PDF situation — entity named in search summaries, row never eyeballed (OFFICIAL-SNIPPET-UNCONFIRMED) |
| **Latency claims** | Qualitative only ("low latency", "blazing fast") — NO ms figure anywhere (OFFICIAL-SNIPPET-UNCONFIRMED / UNVERIFIED as a spec). Uptime marketing 99.995% (snippet) | "Ultra-low latency (milliseconds)" marketing (OFFICIAL-VERIFIED capture) — NO numeric SLA anywhere (UNVERIFIED). Uptime marketing 99.995% (capture). Both: the de-facto floor is the ~1 s snapshot cadence itself |
| **Protocol / SDK quality** | Customer-issued **plaintext `ws://endpoint:port`** — wss/TLS UNVERIFIED (SDK-VERIFIED absence; archive probe gate 4); ZERO heartbeat/ping/reconnect logic in any captured SDK (SDK-VERIFIED grep/AST; server `Echo` frames appear in branch-doc transcripts, cadence undocumented — PRIOR-VERIFIED-NOTE); no resume — full re-auth + re-subscribe per reconnect (PRIOR-VERIFIED-NOTE); 1 WS session/key, new connection kills old (PRIOR-VERIFIED-NOTE); shipped `wsgfdl-py` history call broken (`getbyperiod` TypeError — SDK-VERIFIED AST); static API key auth (SDK-VERIFIED) | **`wss://` TLS**; server heartbeat ~5 s + client reconnect law (Python: ping 10 s + auto-resubscribe; Node: 15 s watchdog / 2 s reconnect) (SDK-VERIFIED + PRIOR-VERIFIED-NOTE #1966); LZ4 compression since v7 (SDK-VERIFIED); `tick_seq` drop detection (SDK-VERIFIED); free post-market/weekend REPLAY host + public sandbox `wstest.truedata.in` (capture + #1966); force-logout recovery endpoint (capture); negatives: credentials in the URL query string (SDK-VERIFIED — redaction burden), 1 login instance (capture), a known `websocket-client` version pitfall (PRIOR-VERIFIED-NOTE) |
| **Symbol caps / limits** | Package tiers ~50–500 concurrent symbols (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog); per-exchange `AllowedInstruments` + calls/bandwidth entitlements via `GetLimitation` (SDK-VERIFIED samples); array pull calls cap at 25 symbols (SDK-VERIFIED); request quotas 1800/3600/7200 per hour by plan, subscribe AND unsubscribe each consume quota (PRIOR-VERIFIED-NOTE) | Plan envelope announced at connect (`maxsymbols`/`subscription`/`validity` — SDK-VERIFIED); 200 symbols default / ~50 trial (COMMUNITY, FYERS); `addsymbol` batched 1500/frame ⇒ large plans exist (PRIOR-VERIFIED-NOTE #1966 high); no per-message WS limit (capture); REST history caps: tick 5/s·300/min·18k/h, bars 10/s·600/min·18k/h (OFFICIAL-SNIPPET-UNCONFIRMED, twice-triangulated) |
| **Historical depth (bundled)** | Tick-class ≈ **7 days** · 1–4 min ≈ **3 months** (5–12 min 4.5 mo; 15 min+ 6 mo) · EOD since 2010 (OFFICIAL-SNIPPET-UNCONFIRMED + PRIOR-VERIFIED-NOTE #1966 medium; the in-SDK `GetLimitation` samples bound tick history to small-integer units — "never years-scale", SDK-VERIFIED reading, gdfl/VERDICT.md §4) | Tick ≈ **5 trading days** (npm README high, #1966) · 1-min ≈ **6 months** · EOD 10+ years (OFFICIAL-SNIPPET-UNCONFIRMED); "Extended History" paid add-on, depth/price UNVERIFIED. Net: GDFL's tick window is slightly LONGER (≈7 d vs 5 d); TrueData's 1-min window is LONGER (6 mo vs ≈3 mo at 1-min); BOTH fail the BruteX 2020 floor by an order of magnitude (feasibility Q1: NO for both — PRIOR-VERIFIED-NOTE) |
| **Greeks + option-chain streams** | Stronger: streamed per-strike greeks at a 16-metric field set, NSE-relayed (not vendor-computed), plus chain+greeks combined subscriptions and strike-windowed chain streams (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog of the PR #1595 branch docs; subscription verbs SDK-VERIFIED in `gdfl/api/WEBSOCKET_API.md`) | 8-field greeks stream (IV/Delta/Theta/Gamma/Vega/Rho), opt-in backend enablement, computed-by-whom UNVERIFIED (SDK-VERIFIED shape); chain = SDK-assembled from subscribed legs + one-shot REST snapshot (PRIOR-VERIFIED-NOTE) |
| **Whole-exchange breadth** | `StreamAllSymbols` short-key firehose (separately sold) + `GetExchangeSnapshot` / `SubscribeExchangeSnapshot` bulk bars over REST+WS (PRIOR-VERIFIED-NOTE + SDK-VERIFIED verb inventory) | Full Market Feed entitlement — whole-exchange-segment snapshot stream on port 9084, no per-symbol subscribe (SDK-VERIFIED(sample) + OFFICIAL-VERIFIED capture product grid). Both have breadth; GDFL adds pull-based bulk bars |
| **Unattended auth / ops** | YES structurally: ONE static API key (WS `Authenticate` frame / REST `accessKey` param), no TOTP, no daily mint (SDK-VERIFIED); ToS/automation terms UNVERIFIED | YES structurally: scriptable password grant (~3600 s token, SDKs auto-re-mint) + WS query-string creds (SDK-VERIFIED); ToS terms UNVERIFIED. Both beat the Zerodha finding (feasibility Q4 — PRIOR-VERIFIED-NOTE) |
| **Dev / regression harness** | None evidenced: no replay, no sandbox in any captured surface (UNVERIFIED-negative) | Free post-market/weekend REPLAY of the full feed (all active subscribers) + public sandbox `wstest.truedata.in` + beta host (OFFICIAL-VERIFIED capture + PRIOR-VERIFIED-NOTE #1966); replay schedule UNVERIFIED |
| **History==live equivalence** | UNVERIFIED — the gdfl/VERDICT.md core integrity probe (gate 3): does `GetHistory` TICK replay what `SubscribeRealtime` streamed? | UNVERIFIED — same question via `/getticks` vs the live stream; nothing in either corpus answers it |
| **Maintenance / connect windows** | Daily 02:00–02:30 + 08:00–08:30 IST (BOD sometimes till 08:45) — connect after 08:45 IST (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog) | Trading weekdays 07:30–08:00; Sat/Sun 07:30–10:30 IST (PRIOR-VERIFIED-NOTE, official KB rows via the 2026-07-16 catalog) |
| **Pricing reachability** | Quote-only; ZERO public figures ("flexible pricing policy" — OFFICIAL-SNIPPET-UNCONFIRMED); the old "plans from ₹3,000" snippet did not re-surface 2026-07-20 → UNVERIFIED; API pricing page blocked in both capture sessions; no-refund ⇒ trial-first (PRIOR-VERIFIED-NOTE) | Custom-quoted after requirements review (OFFICIAL-VERIFIED capture); partner figures exist: ₹1,599+GST/mo tick-streaming API, ₹2,599 combined, 200 symbols default (COMMUNITY, FYERS, undated); ≤10-day free trial + sandbox (OFFICIAL-VERIFIED capture); 10% annual discount (OFFICIAL-SNIPPET-UNCONFIRMED). More reachable, still not vendor-published |

### Context row — vs the two retired in-house live feeds (all PRIOR-VERIFIED-NOTE, 2026-07-16 catalogs)

| Feed | Push class | Timestamp precision | Payload per update |
|---|---|---|---|
| GDFL | ~1 update/s/symbol, full-image conflated L1 | epoch whole-seconds | richest: LTP/LTQ + bid/ask + day OHL + prev-close + VWAP + cum. volume/turnover + OI + changes, inline every push |
| TrueData | ~1-second-class coalesced L1, sequenced (`tick_seq`), event-on-change reading | second-granular (ms-vs-s on wire UNVERIFIED) | rich: LTP/LTQ/ATP/TTQ + day OHL + prev close + OI + turnover + special tag + seq + L1 bid/ask |
| Dhan WS (retired 2026-07-13) | ~2–4 conflated updates/s/symbol (sampled) | whole-second LTT | binary packets; day OHLC in Quote; OI as a separate packet; 5-level depth in Full mode |
| Groww WS (retired 2026-07-15) | per-LTP-event push, price-only | **milliseconds** | `{ltp, ts}` only — no volume/OI/depth |

Reading: neither candidate beats Dhan's measured update RATE or Groww's timestamp
precision; both beat both on per-message field richness. No retail feed in this
table is tick-by-tick — the class ceiling is the constant.

## §3 The winner + the deciding evidence

**For a live L1/depth snapshot socket, TrueData wins on engineering surface + ops;
on NSE data CONTENT the two are EQUIVALENT-CLASS (~1-second coalesced L1) — so this
is a surface win, not a granularity win.**

Deciding evidence, in order of weight:

1. **Transport + liveness discipline** (the row that decides a production feed):
   TrueData ships wss TLS, a ~5 s server heartbeat, a client reconnect law that
   re-subscribes, LZ4, per-symbol `tick_seq` gap detection, a free replay host
   (a real dev/regression harness), a public sandbox, and a force-logout recovery
   endpoint (SDK-VERIFIED + capture). GDFL ships a customer-issued **plaintext**
   `ws://` endpoint (TLS unverified), ZERO heartbeat/reconnect logic in any SDK,
   no resume, and its newest WS package's history call is broken as shipped
   (SDK-VERIFIED) — the GDFL archive's own integration advice is "speak the wire
   protocol directly; use the SDKs as protocol documentation".
2. **The in-repo feasibility synthesis agrees** — its Q2 (live-feed fit for
   TickVault) verdict is: GDF **"PARTIAL"**; TrueData **"YES (strongest surface
   studied)"**, and its recommended order is TrueData trial first, GDF second
   (PRIOR-VERIFIED-NOTE, `docs/research/GDF_TRUEDATA_FEASIBILITY.md` §1/§6,
   2026-07-15). The 2026-07-20 research refines its "true tick stream" phrasing
   (see `truedata/README.md` supersede note) without changing the Q2 surface
   verdict.
3. **The one genuine DATA differentiator is BSE-only**: TrueData's `bidaskL2`
   top-5 depth (SDK-VERIFIED). On NSE — the segment BruteX/TickVault care about —
   both feeds are L1-only at ~1 s (OFFICIAL-VERIFIED both, §2 row 2), so no data
   argument separates them there.

**GDFL counterpoints (honest — where GDFL is stronger):**

- **Greeks + chain surface**: streamed per-strike greeks at a 16-metric field set
  (IV/Delta/Theta/Vega/Gamma/IVVwap/Vanna/Charm/Speed/Zomma/Color/Volga/Veta +
  ratio metrics), NSE-relayed rather than vendor-computed, plus chain+greeks
  combined subscriptions and strike-windowed chain streams
  (PRIOR-VERIFIED-NOTE, 2026-07-16 catalog of the PR #1595 branch docs; the
  subscription verbs themselves are SDK-VERIFIED in `gdfl/api/WEBSOCKET_API.md`).
  TrueData's greeks stream is an 8-field opt-in add-on (SDK-VERIFIED). (TrueData
  separately markets a "16+ matrices" greeks API — UNVERIFIED; its shipped SDK
  delivers 8 fields.)
- **Whole-exchange + pull breadth**: `StreamAllSymbols` short-key firehose +
  `GetExchangeSnapshot`/`SubscribeExchangeSnapshot` bulk-bar surface
  (PRIOR-VERIFIED-NOTE + SDK-VERIFIED verb inventory). TrueData's equivalent is
  the Full Market Feed entitlement (port 9084, SDK-VERIFIED(sample)) — both have
  breadth; GDFL adds pull-based bulk bars over REST.
- **Per-message lag observability**: GDFL frames carry `ServerTime` beside
  `LastTradeTime` — a built-in per-message lag probe (PRIOR-VERIFIED-NOTE);
  TrueData has no equivalent second clock on the tick.
- **Tick-history window**: ≈7 days vs TrueData's ≈5 (both snippet-tier; both
  useless for backfill).
- **The FIX tier**: GDFL markets an undocumented, contact-sales FIX API claiming
  "tick-by-tick" delivery — UNVERIFIED whether that is genuine exchange TBT or the
  same 1 s feed over FIX transport (PRIOR-VERIFIED-NOTE; sales question in
  writing). It is the ONLY possibly-finer tier either vendor names.

**Next actions (restating the standing feasibility recommendation, 2026-07-15 —
PRIOR-VERIFIED-NOTE, unchanged by this research):** pursue in order — (1) the cheap
≤10-day TrueData trial probe (pin measured cadence, Extended-History depth/price,
span caps, VIX symbol, latency, port map); (2) TickVault live-feed evaluation with
TrueData FIRST, GDFL second; (3) a GDFL sales conversation only if (1) fails —
folding in the FIX-tier and wss/TLS questions in writing. Nothing here is a build
authorization.

Standing scope note: this doc authorizes nothing. Any capture leg stays OUT of
BruteX (DEC-LIVE-REMOVE-001) and TickVault-side adoption needs its own DEC + the
probe gates below; the BruteX lake stays Groww (feasibility Q1 — both vendors NO).
If a capture leg is ever designed, the GDFL ingest design's §9.5 inherited
constraints (per-source subtree separation, declared epoch units, gap records,
staleness watchdogs, 1-session single-flight, credential redaction, own
market-hours guard) apply to EITHER vendor — `gdfl/INGEST_DESIGN.md`
(PRIOR-VERIFIED-NOTE, DEC-GDFL-INGEST-DESIGN-001 #1781).

## §4 What would change the verdict

1. **Credentialed cadence probes** (both vendors; trial keys): measured msgs/sec
   per segment, `tick_seq` gap behavior (TrueData), emit-on-change vs heartbeat on
   quiet symbols + indices (both). If either showed sustained multi-Hz per-symbol
   pushes with genuinely distinct intra-second trade content, the equivalent-class
   ruling would need re-examination (TrueData's wire cannot carry sub-second
   timestamps regardless — SDK-VERIFIED).
2. **The NSE PDFs read first-hand** (operator upload — `truedata/MANIFEST.md`
   items 1–2): the authorized-vendor rows for both entities, the TBT tariff tier's
   real figures, whether a TBT row exists for F&O, and resolution of the
   "not through DotEx" vs priced-vendor-TBT-tier tension.
3. **GDFL FIX-tier disclosure** (sales, in writing): if it is real order-level TBT
   at a retail-reachable price, the §1(a) "nobody" answer changes.
4. **TrueData Extended-History quote**: if 1-min genuinely reaches 2020 at sane
   cost, the (separate) LAKE question reopens — the live verdict is unaffected.
5. **GDFL wss/TLS + heartbeat confirmation** on the issued endpoint: would close
   TrueData's two biggest surface leads (the win would then rest on replay +
   `tick_seq` + BSE L2 + SDK quality alone).
6. **Multi-login purchasability** (both): either vendor offering >1 concurrent
   session would ease the ops story for redundant capture.

## §5 Consolidated UNVERIFIED / BLOCKED register (both vendors + NSE-side)

NSE-side note: ALL NSE hosts were proxy-blocked in every session — nothing
NSE-official was ever byte-read; every NSE fact in this doc is snippet tier.

| Side | Open item (status) | What closes it |
|---|---|---|
| NSE | Authorized-vendor list rows for BOTH entities (search summaries only — OFFICIAL-SNIPPET-UNCONFIRMED) | One off-proxy download of the nsearchives PDFs (`truedata/MANIFEST.md` upload item 1) |
| NSE | TBT tariff figures (~₹50L/yr CM + per-user fees) + whether a vendor TBT tier exists for F&O; the "not through DotEx" vs tariff-tier tension (OFFICIAL-SNIPPET-UNCONFIRMED) | Read Real_Time_Tariff_Domestic_01042025_.pdf + the real-time-data-subscription page (upload item 2) |
| NSE | Official MAINLAND vendor-broadcast cadence figure (1/s is NSEIX-official + broker/community convergence only — UNVERIFIED as mainland-official) | The Realtime_Technical_Specification / Snapshot MDR spec PDFs |
| NSE | Current (2026) F&O vendor-TBT availability (the ~2020 "stopped" statement is COMMUNITY) | NSE circulars / a vendor sales question in writing |
| GDFL | Emit-on-change vs heartbeat in the 1 s cadence; per-symbol every-second delivery under burst (UNMEASURED) | Credentialed live capture (gdfl/VERDICT.md gates 5–7 + the 2026-07-16 catalog probes) |
| GDFL | wss/TLS acceptance; session policy restated; real rate/throttle values (UNVERIFIED) | Probe gates 4–6 on an issued endpoint + sales |
| GDFL | Depth-history numbers (≈7 d / ≈3 mo / 2010) unconfirmed (OFFICIAL-SNIPPET-UNCONFIRMED; SDK samples bound "never years-scale") | Upload the type-of-data-available page + GetLimitation on a real key |
| GDFL | FIX tier — real TBT or the 1 s feed over FIX transport? (UNVERIFIED) | Sales question in writing |
| GDFL | Any pricing figure; subscriber-agreement licensing terms (UNVERIFIED) | Upload the api-pricing page; obtain the agreement from sales |
| GDFL | Absence-of-depth-product is a search-negative, not proof (UNVERIFIED-negative) | Upload the `/apis/` + type-of-data pages |
| TrueData | Measured msgs/sec per segment; wire timestamp precision; raw JSON keys (API PDF v2.2.1 unretrieved in every session) (UNVERIFIED) | Trial probe gates 1–4 (`truedata/VERDICT.md`) + upload item 3 |
| TrueData | Extended-History depth + price — the decisive lake question (UNVERIFIED) | sales@truedata.in quote (upload item 5) |
| TrueData | Numeric plan/symbol tiers beyond 200/50 (COMMUNITY); vendor-published pricing (FYERS figures COMMUNITY, undated); daily REST cap existence (UNVERIFIED) | Sales + the /price / KB uploads (items 4+6) |
| TrueData | Latency SLA; INDIA VIX servability; expired-contract history; replay schedule; port→plan map; BSE L2 order-count field naming (UNVERIFIED) | Trial probes 5–9 + the gated API PDF |

Blocked-host classes (2026-07-20 session): `globaldatafeeds.in` (8/8 retries incl.
2 new URLs), `*.truedata.in` family, `nseindia.com`/`nsearchives`/`archives`,
`niftyindices.com`, `fyers.in`, `marketcalls.in`, `web.archive.org` — the working
official-material channel both sessions was PyPI (JSON API + pip). Full logs:
`truedata/MANIFEST.md` (TrueData/NSE) + `gdfl/MANIFEST.md` (the 2026-07-13 63-URL
GDFL table; the 2026-07-20 GDFL retry log lives in the session scratchpad and adds
three newly-discovered blocked official URLs + the NSE PDF avenue to the upload
list).

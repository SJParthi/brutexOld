# GDF (Global Datafeeds) + TrueData as BruteX/TickVault data sources — research synthesis

**Date of research:** 2026-07-15 (all vendor facts as-of this date unless stamped otherwise).
**Scope:** data-source research ONLY. Zero engine implications, zero code changes beyond docs.
The brute-force ENGINE stays NIFTY/BANKNIFTY/SENSEX regardless of vendor (CLAUDE.md §2 Rule 3).

**Retrieval honesty (Golden Rule #1/#10):** every vendor host (`globaldatafeeds.in`,
`*.lisuns.com`, `*.truedata.in`) was egress-blocked from this build environment (agent-proxy
CONNECT 403). Vendor-PAGE evidence is WebSearch server-side relay (medium confidence);
the load-bearing protocol/endpoint evidence is **vendor-published SDK packages fetched
directly** (GDF: PyPI `GFDLWS`/`ws-gfdl`/`gfdl-rest`; TrueData: PyPI `truedata`/`truedata-ws`,
npm `truedata-nodejs`, NuGet `TrueData-DotNet` — high confidence). No raw vendor page was
byte-read in-session; a credentialed live probe is mandatory before any of this is encoded
as fact in code.

**Full doc packs (every vendor doc section, per-claim sources + confidence):**
`docs/gdf/api-reference/` (README + 11 sections) and `docs/truedata/api-reference/`
(README + 10 sections). This synthesis only ranks and concludes; the packs carry the detail.
Both packs are self-contained and copyable verbatim into the TickVault repo.

---

## 1. Executive verdict

| # | Sub-question | GDF verdict | TrueData verdict |
|---|---|---|---|
| Q1 | 1-minute depth for BruteX's 2020→today lake (indices + ~750-symbol universe) | **NO (as bundled)** — API 1-min depth ≈ 3 months (5–10 min 4.5 mo; 15-min+ 6 mo; D/W/M since 2010 — vendor page, medium). Deep 1-min (NSE F&O since 2010-09-01) exists only as purchased IEOD FILE packs (low conf.), spot-index coverage of those packs UNVERIFIED. Per-key deep enablement on the API: UNVERIFIED | **NO (as bundled)** — 1-min bars last 6 months, tick last 5 trading days, EOD 10+ years (vendor KB, medium; tick 5d also npm README, high). Longer = "Extended History" PAID ADD-ON whose depth/price are UNVERIFIED — the decisive sales/probe question |
| Q2 | Live-feed fit for TickVault | **PARTIAL** — 1-second snapshot stream (`SubscribeRealtime`), L1-only book, streamed 1-min bars (`SubscribeSnapshot`), whole-exchange bulk bars (`GetExchangeSnapshot`), rich 16-field Greeks streams; "tick" rows are second-stamped, not raw exchange ticks (medium inference) | **YES (strongest surface studied)** — true tick stream with tick sequence numbers + bid/ask + OI, server-side 1-min/5-min bars, full-market feed (port 9084), BSE L2 depth, greeks stream, heartbeats, AND a free post-market/weekend REPLAY service (dev harness). Latency SLA UNVERIFIED |
| Q3 | Expired F&O contract history (the Groww-unique BruteX asset) | **UNVERIFIED** — continuous `NIFTY-I/-II/-III` aliases exist; per-expired-contract API queryability never evidenced | **UNVERIFIED** — same shape: continuous aliases evidenced, expired-contract queries not |
| Q4 | Unattended-auth compatibility (BruteX §11 zero-click decree) | **YES (structurally)** — one static API key; WS `Authenticate` message / REST `accessKey` param; no daily interactive step evidenced. ToS/automation terms UNVERIFIED | **YES (structurally)** — scriptable OAuth-style password grant (~1 h token, SDKs auto-re-mint); WS query-string auth; no daily interactive step evidenced. ToS terms UNVERIFIED |
| Q5 | Documented rate limits → pull arithmetic | Per-hour quotas (FAQ examples 1800/3600/7200 req/h, plan-based — medium); per-second cap UNVERIFIED; per-request span cap UNVERIFIED ⇒ honest pull arithmetic NOT derivable yet | REST tick 5/s·300/min·18,000/h; bars 10/s·600/min·18,000/h (KB, medium, twice-triangulated); per-request span cap UNVERIFIED ⇒ only a conditional envelope derivable (§3) |
| Q6 | Pricing | **UNVERIFIED** — quote-gated ("flexible pricing policy"); the circulating ₹70k/yr figure is a STALE 2019 desktop-product reference, not API pricing | **UNVERIFIED** — custom-quoted after requirements review; ≤10-day free trial + sandbox; "10% discount on 1 year subscriptions" (medium). No numerics retrievable |
| Q7 | Data quality / conventions | Two timestamp conventions (WS epoch-seconds vs REST epoch-MILLISECONDS + ALL-CAPS); per-key entitlement surface (GetLimitation) means capability varies per account; CA-adjustment policy UNVERIFIED; India VIX identifier UNVERIFIED | Bar labeling = bucket-open minute (matches BruteX's resampler convention — a real compatibility point); corp-action endpoints exist but bar ADJUSTMENT policy UNVERIFIED; India VIX symbol UNVERIFIED; `&`-symbols evidenced with SDK-side `%26` encoding |

**Net:** NEITHER vendor, as publicly documented, can replace Groww for the BruteX lake:
the bundled 1-minute history (GDF ~3 months, TrueData 6 months) is an order of magnitude
short of the 2020→today floor, and the deep-history paths (GDF IEOD file packs, TrueData
Extended-History add-on) are unpriced/unverified for exactly the spot-index +
whole-universe shape BruteX needs. Both are, however, **credible live-feed vendors** —
TrueData's tick-level WebSocket (with replay) is the natural TickVault-side candidate to
probe, and GDF's whole-exchange snapshot + per-key entitlement model suits bulk 1-second
capture. Both support unattended auth structurally — the single dimension where both beat
the Zerodha finding (`docs/research/ZERODHA_HISTORICAL_FEASIBILITY.md` Q6: NO).

---

## 2. Claim highlights (the load-bearing rows only — full tables live in the packs)

Confidence legend: high = vendor-authored artifact fetched directly (or triangulated
incl. one vendor-authored source) · medium = search-relayed vendor content · low =
third-party only · UNVERIFIED = not retrievable. All access dates 2026-07-15.

| Claim | GDF | TrueData |
|---|---|---|
| Auth | ONE API key; WS `{"MessageType":"Authenticate","Password":<key>}`; REST `accessKey=` param (high — vendor pkgs) | `POST auth.truedata.in/token` password grant → Bearer (~3600 s); WS `?user=&password=`; utility API plain params (high — vendor pkgs) |
| Endpoints | `ws://nimblewebstream.lisuns.com:4575`, `http://nimblerest.lisuns.com:4531` / TLS `:4532` (medium — 2–4 indep. repos + vendor-FAQ port test); vendor: "endpoint … will be provided" (high); TLS WS UNVERIFIED | `wss://push.truedata.in:8082/8084` (+ replay host, full-feed 9084, corp 9092), `history.truedata.in`, `api.truedata.in`, `analytics/greeks/corporate.truedata.in` (high — vendor pkgs) |
| History call | `GetHistory` (WS+REST): Periodicity TICK/MINUTE/HOUR/DAY/WEEK/MONTH × Period, From/To epoch-seconds or Max-N (high) | `/getbars /getticks /getlastnbars /getlastnticks /getbhavcopy(+status) /gettopn* /getcorpaction /getsymbolchangehistory`; `YYMMDDTHH:MM:SS`; intervals 1–60 min + EOD/week/month (high) |
| Symbology | `FUTIDX_NIFTY_28JUL2022_XX_0` / `OPTIDX_NIFTY_21JUL2022_CE_12900`; continuous `NIFTY-I/-II/-III`; short ids `NIFTY25MAR2115000CE`; numeric TokenNumber for Greeks; THREE expiry casings (high) | Spot indices WITH SPACES (`NIFTY 50`, `NIFTY BANK`); continuous `-I/-II/-III`; API option symbols `{SYM}{YYMMDD}{STRIKE}{CE|PE}`; one recorded blog-variant conflict (`NIFTY20000C1`, platform-side) (high + recorded conflict) |
| Bulk facilities | `GetExchangeSnapshot` (whole exchange, one call — shape UNVERIFIED beyond envelope); array calls capped 25 | Full-market feed entitlement; `addsymbol` batched 1500/frame; bhavcopy per segment |
| Entitlements | `GetLimitation` per-key dump: exchanges, functions, per-interval history enables, MaxTicks/MaxEOD/MaxIntraday, calls+bandwidth per hour/month, key expiry (high) | Plan attributes: symbol count, streams (tick+1min/5min), segments (exchange-approval gated — vendor verbatim: "data that is not enabled by default may require exchange approvals") (high) |
| Vendor SDK quality | Alpha-grade (typos, inverted branch, one wrong REST route in gfdl-rest) — protocol reference only (high) | Maintained SDKs (Py 7.0.1 2025-07 · Node 1.2.0 2026-05 · .NET 1.4.7 2025-07); dead PyPI homepage link recorded (high) |

---

## 3. Pull-time arithmetic — honestly NOT derivable yet (and the conditional envelope)

For Zerodha this synthesis's sibling could compute wall-clock bounds because BOTH the
rate cap AND the per-request span cap were evidenced. Here neither vendor's
**per-request span cap** was retrievable (GDF: `Max` record caps are per-KEY
entitlements; TrueData: SDKs pass arbitrary from/to with no cap evidenced) — so honest
requests-per-symbol figures cannot be derived. What CAN be bounded, conditionally:

- **TrueData, IF** the Extended-History add-on served 2020-01-01→today 1-min AND a
  request span were capped at ~1 month: ~79 requests/symbol (2020-01 → 2026-07
  inclusive ≈ 79 months) × ~750 symbols ≈ 59,250 requests; the binding constraint is
  the 18,000/hour cap (not the 10/s) ⇒ ≈ **3.3 h** whole-universe backfill; the 4
  index/reference instruments alone ≈ 316 requests ≈ **32 s** at 10 r/s. DERIVED from
  unverified premises — an illustration of feasibility-if-sold, not a claim.
- **GDF:** no bound derivable — bundled depth (~3 months) makes a 2020 backfill
  structurally impossible via the API as evidenced; the IEOD path is a FILE purchase
  (import job, not a pull), and per-hour quotas are plan-based.

Both vendors' numbers here must be re-derived after a live `GetLimitation` (GDF) /
trial-sandbox probe (TrueData) pins the real span caps and entitlements.

---

## 4. Integration sketch (hypothetical — NO code changes in this record)

- Either vendor would be a second adapter package (`brutex/adapters/gdf/` /
  `brutex/adapters/truedata/`) mirroring `brutex/adapters/groww/`'s shape; the lake
  layout is vendor-agnostic (`ParquetBarSink`), prices snap to the 2-dp grid at the
  write boundary exactly as today.
- **Rate control: REUSE `brutex/adapters/rate_common.py`** — injected ceilings:
  TrueData `ceiling_rps=10.0` (bars) / `5.0` (ticks) + a NEW per-hour governor need
  (18,000/h — the existing controller has per-second + per-minute; an hourly window
  would be a small extension); GDF has no per-second figure — the per-hour quota
  (plan-based) would drive a conservative injected ceiling until probed.
- **Parse boundaries:** GDF needs the WS-seconds vs REST-milliseconds + ALL-CAPS
  normalisation; TrueData needs the `YYMMDDTHH:MM:SS` builder + optional LZ4-block
  decompression (`comp=true`) + the CSV column maps.
- **Symbol mapping:** both need explicit index-name maps (GDF `NIFTY BANK` (NSE_IDX)
  vs product `BANKNIFTY`; TrueData `NIFTY 50`/`NIFTY BANK` spot vs `NIFTY-I` futures)
  onto BruteX's lake keys — the same alias-mapping class `brutex/universe.py` already
  handles for Groww.
- **TickVault side:** TrueData's feed maps naturally onto tick capture (tick_seq
  numbers make gap detection mechanical); the replay service doubles as a
  deterministic integration-test harness. GDF's `GetExchangeSnapshot` +
  `SubscribeSnapshot(MINUTE,1)` suit bulk 1-second/1-minute capture with fewer
  subscriptions.
- **ENGINE UNCHANGED** in every scenario.

---

## 5. Risks (blunt)

- **Depth risk (BruteX-fatal until disproven):** both vendors' bundled intraday
  history is months-deep, not years-deep. Any BruteX adoption hangs ENTIRELY on
  unpriced, unverified add-ons (TrueData Extended History / GDF IEOD-or-per-key
  enablement) whose spot-INDEX coverage is itself unverified.
- **Entitlement opacity:** both vendors gate capability per key/plan (GDF
  GetLimitation axes; TrueData exchange-approval + backend-enablement language) —
  public docs describe the UNION of what CAN be sold, not what a given account gets.
  Every capability must be probed on the actual purchased key.
- **Transport/security:** GDF third-party usage runs plain `ws://`+`http://` with the
  key in the URL (TLS WS unverified); TrueData puts the password in URLs on two of its
  three auth patterns. Both need secret-hygiene discipline (SSM-style storage, URL
  scrubbing in logs).
- **Docs-vs-reality gaps:** GDF's own PyPI clients carry bugs (wrong route, inverted
  branch); TrueData's PyPI homepage is a dead link and its official PDF was
  unretrievable. The wire protocols are well-evidenced, but operational numbers
  (latency, caps, depth) lean on relayed KB/FAQ pages that can drift without notice.
- **Quality unknowns:** corporate-action adjustment policy unverified at BOTH
  (mutable-history risk for the immutable keep-first lake — same class as the Zerodha
  finding); GDF "tick" is second-stamped snapshots; TrueData latency SLA unverified;
  India VIX identifier unverified at both (BruteX stamps `vix_at_entry/exit` — a
  required probe).

---

## 6. Recommendation

**Default: NO BruteX lake adoption on current evidence.** The bundled-depth shortfall
is structural, and Groww (probe-verified 2020-01-01 1-min floor, expired-options
1-minute, working unattended auth) remains strictly superior for the lake today.

**Worth pursuing, in order:**
1. **TrueData trial probe (cheap: ≤10-day free trial + sandbox)** — pin: Extended
   History depth/price for spot indices + the ~750-symbol universe at 1-min;
   per-request span cap; expired-contract queryability; India VIX symbol; CA policy;
   latency; the port→plan map. If the add-on genuinely serves 2015→2020-class 1-min
   spot-index history at sane cost, it becomes the supplemental-backfill candidate
   (the role Zerodha failed on auth grounds — TrueData has no such auth conflict).
2. **TickVault live-feed evaluation** — TrueData first (tick+seq+replay), GDF second
   (snapshot/bulk model); both structurally compatible with unattended operation.
3. **GDF sales conversation** only if (1) fails — specifically whether deep 1-min can
   be enabled per-key ON the API (not as IEOD files), and what the spot-index IEOD
   coverage actually is.

**Mandatory pre-decision live probes** (consolidated; each pack carries the full list):
GDF — `GetLimitation` + `GetExchanges` + `GetInstruments('NSE_IDX')` + a GetHistory
depth bisect + TLS-WS question. TrueData — trial + sandbox: depth bisect per segment,
span-cap pin, `getAllSymbols` VIX grep, replay schedule, latency measurement.

Any adoption ADR must restate the depth findings bluntly, carry the per-claim
confidence stamps from the packs, and record that pricing was quote-gated at research
time (never assume a figure that isn't on a dated quote).

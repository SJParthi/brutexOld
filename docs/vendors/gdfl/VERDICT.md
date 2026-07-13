# GDFL granularity + depth — the definitive verdict

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780). Captured/measured
> 2026-07-13. Evidence tiers per `README.md`. Nothing below is copied from vendor
> prose; identifiers (`TICK`, `GetHistory`, field names) are vendor API tokens kept
> verbatim by necessity.

## Verdict (headline)

**GDFL's finest history granularity is 1-second L1 snapshot data (vendor enum:
`TICK`) — irregular within-second cadence, not a fixed one-row-per-second grid, and
not sub-second exchange tick-by-tick — and that 1-second class is the CEILING for
Indian retail data, not a compromise. No 1-second OHLC bar periodicity exists
anywhere in the API. The 1-second-class history window is days-scale (per the
UNCONFIRMED official snippet — and never years-scale under ANY reading of the
in-SDK evidence, §4), so a 2022→now 1-second-class BACKFILL is NOT available from
GDFL; forward capture from subscription day is the only 1-second-class path.**

Terminology note (operator-directed): we call this data **"1-second L1 snapshot
data"** throughout. Each record is a Level-1 quote/trade snapshot (last trade price,
one best bid + one best ask with sizes, per-print traded quantity, open interest)
stamped at second resolution (HH:MM:SS). In the raw form a busy second can carry
several snapshots and a quiet stretch carries none; collapsed to one snapshot per
second it is a clean 1-second series. GDFL's own API calls this periodicity `TICK`
(and its history frame `HistoryTickResult`) — where those vendor identifiers appear
below they mean exactly these 1-second L1 snapshots.

## Why 1-second is the ceiling, not a compromise

Prior verified team research (2026-07-09, tick-feed research note — 3 research passes
+ 8 adversarial verifiers): **1-second is the finest REAL granularity available for
Indian markets at retail.** NSE distributes a ~1-second conflated snapshot broadcast;
none of the retail broker/vendor websockets (Zerodha/Upstox/Angel/Groww/Fyers/GDFL/
TrueData class) delivers more than that, and genuine sub-second tick-by-tick requires
NSE MTBT exchange membership (members-only, lakhs/yr; no authorized vendor sells
decoded full-book TBT to non-members). Index values (NIFTY/BANKNIFTY/SENSEX) have no
trade tape at all — a ~1/s broadcast ceiling. GDFL's 1-second L1 snapshot data is
therefore **the optimal achievable granularity for Indian retail** — the depth
question (how far back it goes) is the make-or-break, and that is where GDFL falls
short for backfill.

## The facts

### 1. History granularity menu (SDK-VERIFIED)

`GetHistory` accepts exactly SIX periodicities — **`TICK` / `MINUTE` / `HOUR` /
`DAY` / `WEEK` / `MONTH`, default `TICK`** — with an integer `Period` multiplier
("1, 2, 3…") applicable to `MINUTE`/`HOUR`/`DAY` only. `From`/`To` are Unix epoch
**seconds**. Verified identically in the gfdl-rest@1.0.3 and wsgfdl-py@1.3.5
vendor references + code (grep + AST over the captured sources).
**There is NO 1-second (or any sub-minute non-`TICK`) periodicity anywhere** — no
1-second OHLC bars exist; the only sub-minute product is the 1-second L1 snapshot
stream/history described above.

### 2. Record shapes (SDK-VERIFIED, field names verbatim)

- 1-second L1 snapshot history record (vendor `TICK` / `HistoryTickResult`):
  `LastTradePrice`, `LastTradeTime`, `BuyPrice`, `BuyQty`, `SellPrice`, `SellQty`,
  `TradedQty`, `OpenInterest`, `QuotationLot` — **L1 only** (one bid, one ask; no
  depth levels), no OHLC on these records.
- OHLC history record (`MINUTE`+ / `HistoryOHLCResult`): `Open`, `High`, `Low`,
  `Close`, `TradedQty`, `OpenInterest`, `LastTradeTime` (+ `QuotationLot`).
- Result frames: `HistoryTickResult` / `HistoryOHLCResult` (WS); the REST twin
  returns the same records with UPPERCASE keys under `TICK:` / `OHLC:` wrappers.

### 3. What the data actually looks like (RAW-SAMPLE-MEASURED)

Coordinator worker's raw-byte measurement of the operator's 5 real GDFL NFO sample
CSV files, 2026-07-13 (measured outside this container; statistics recorded here,
raw rows NOT committed):

- Schema (header verbatim): `Ticker,Date,Time,LTP,BuyPrice,BuyQty,SellPrice,SellQty,LTQ,OpenInterest`.
- One trading day (01-Jul-2025), 09:15–15:30 IST, IST-naive timestamps,
  **second resolution** (HH:MM:SS).
- **NO OHLC columns, NO cumulative volume column**; `OpenInterest` present on EVERY
  row; `LTQ` non-zero ONLY on trade prints (quote-only snapshots carry `LTQ`=0).
- Cadence is **IRREGULAR** — snapshot-driven, not a fixed 1-row-per-second grid:

| Sample file | Rows | Max rows in one second | Gap profile |
|---|---|---|---|
| NIFTY25200CE | 13,005 | 4 | 2 s×8,237 · 1 s×4,236 · 3 s×299 · 4 s×115 · 5 s+ tail; 40 same-second (0-gap) rows |
| NIFTY26050PE | 9,662 | 4 | same shape, sparser |
| OFSS9300CE | 13,245 | 4 | early-session gaps 12–30 s |
| OIL-I (future) | 7,771 | 4 | early-session gaps ~25–28 s |
| MIDCPNIFTY-I (future) | 13,655 | 5 | mostly 1–2 s |

- The `-I` files are futures (GDFL current-contract convention) — schema-identical
  to the options files.
- Whether a row is emitted ONLY on change vs. also as a heartbeat is **UNMEASURED**
  (the identical-consecutive-row count was not taken).

### 4. History depth (the make-or-break)

- **NOT STATED IN SDK:** no captured SDK or vendor README states any history depth
  in prose (grep-verified across all seven package trees).
- The ONLY in-SDK evidence is the `GetLimitation` sample responses embedded in the
  vendor READMEs: `HistoryLimitation: { MaxEOD: 100000, MaxIntraday: 44, MaxTicks: 7, … }`
  (REST sample) vs `MaxTicks: 5` (WS sample). **Units are NOT STATED**, and the
  7-vs-5 divergence shows these are per-account entitlements, not global constants
  (SDK-VERIFIED that the samples say this; the meaning needs a live probe).
- OFFICIAL-SNIPPET-UNCONFIRMED (site egress-blocked; search snippets only):
  1-second-class (the vendor's `TICK` class) history ≈ 7 days, 1-minute ≈
  3 months, EOD since 2010. Consistent with `MaxTicks: 7` read as days, but
  UNCONFIRMED.
- What each source actually supports: the units-unknown `GetLimitation` samples
  bound the window to a SMALL-INTEGER count (7 / 5) of SOME unit — at most
  months-scale under any plausible reading, **never years-scale**; only the
  UNCONFIRMED official snippet pins it at ≈7 DAYS. Every reading rules out a
  multi-year backfill.

## Evidence table

| # | Claim | Verdict | Tier | Source |
|---|---|---|---|---|
| 1 | GetHistory periodicity enum = TICK/MINUTE/HOUR/DAY/WEEK/MONTH (default TICK); integer Period for MINUTE/HOUR/DAY only; From/To epoch seconds | CONFIRMED | SDK-VERIFIED | gfdl-rest@1.0.3 + wsgfdl-py@1.3.5 references + code (captured 2026-07-13) |
| 2 | NO 1-second bar periodicity exists anywhere in the API | CONFIRMED | SDK-VERIFIED | grep over all captured SDK trees + both vendor references |
| 3 | Vendor `TICK` = 1-second-resolution L1 quote/trade snapshots (LTP + best bid/ask + sizes + per-print qty + OI), not OHLC, not sub-second exchange tick-by-tick | CONFIRMED | SDK-VERIFIED + RAW-SAMPLE-MEASURED | `HistoryTickResult` field set; sample CSV schema + cadence measurement |
| 4 | Cadence is irregular (multi-row busy seconds, multi-second quiet gaps), max 4–5 rows/s observed | CONFIRMED | RAW-SAMPLE-MEASURED | 5 NFO sample CSVs, 2026-07-13 |
| 5 | OI on every snapshot row; LTQ non-zero only on trade prints; no cumulative volume column | CONFIRMED | RAW-SAMPLE-MEASURED | same measurement |
| 6 | 1-second-class history depth is days-scale (≈7 days) | SUPPORTED, unconfirmed | GetLimitation samples (SDK-VERIFIED as samples; units NOT STATED) + OFFICIAL-SNIPPET-UNCONFIRMED | READMEs' MaxTicks 7 / 5; blocked official depth page |
| 7 | 1-min ≈ 3 months, EOD since 2010 | UNCONFIRMED | OFFICIAL-SNIPPET-UNCONFIRMED | search snippets of the blocked type-of-data-available page |
| 8 | 1-second is the finest real granularity for Indian retail (NSE ~1 s conflated broadcast; sub-second = MTBT members only) | CONFIRMED (prior) | PRIOR-VERIFIED-NOTE | tick-feed research, 2026-07-09 |
| 9 | Live WS `SubscribeRealtime` pushes ~1/s L1 per instrument incl. per-contract options (bid/ask + OI) | CONFIRMED | SDK-VERIFIED | wsgfdl-py reference ("1 second frequency") + `RealtimeResult` fields |
| 10 | wsgfdl-py 1.3.4/1.3.5 `history.getbyperiod` raises TypeError as shipped (9 args → 10-param zero-default coroutine) | CONFIRMED | SDK-VERIFIED (AST) | captured wsgfdl-py sources; 2022 lineage unaffected |
| 11 | Auth: REST `accessKey` query param per GET; WS first frame `{"MessageType":"Authenticate","Password":"<key>"}`; customer-issued endpoint:port; no TOTP/daily mint | CONFIRMED | SDK-VERIFIED | both SDK code paths + references |
| 12 | 1 WS session per key | CARRIED | PRIOR-VERIFIED-NOTE | GDFL verification note 2026-07-12; NOT restated in the captured SDKs |
| 13 | TLS (wss/https) support | UNVERIFIED | — | WS examples are plaintext ws:// only; REST reference names both http and https endpoint forms |
| 14 | Licensing: "personal use" + single-subscriber clauses; S3/re-publication rights; rate card; refunds | Personal-use/single-subscriber = prior-verified; redistribution + rate card UNVERIFIED; no-refund ⇒ trial-first | PRIOR-VERIFIED-NOTE / UNVERIFIED | GDFL verification note 2026-07-12 |

## Implications for BruteX

A 2022→now 1-second-class backfill from GDFL is **impossible**: the finest history
class is days-deep (exact figure unconfirmed; at most months-deep under ANY reading
of §4 — never 2022-deep), so historical parity with a 1-second-class live record
cannot be reconstructed after the fact. The ONLY way to own a long 1-second L1 snapshot
series is to **capture it forward** from subscription day (live `SubscribeRealtime`
recording, or the vendor's own history pulled within its days-scale window as a
gap-filler). That capture leg — if ever built — stays OUT of the BruteX engine per
DEC-LIVE-REMOVE-001 and belongs to the sibling ingest-design deliverable
(`INGEST_POINTER.md`). For minute-class data GDFL adds nothing BruteX's Groww lake
does not already have deeper (Groww 1-minute reaches 2020; GDFL 1-minute is months-
scale at best, unconfirmed).

SDK quality reinforces a wire-protocol-direct integration: the current WS package's
own `history.getbyperiod` is broken as shipped (evidence row 10), so the SDKs should
be treated as protocol documentation, not dependencies (`sdk/PACKAGES.md`).

## Credentialed probe gates (pre-adoption; each needs a real key + endpoint)

The prior session's 16-gate list was delivered in an ephemeral scratchpad; this is
the reconstructed core. No GDFL adoption decision may claim these — every one is
OPEN until a live probe transcript lands:

1. **GetLimitation real values + units** for the purchased plan (MaxTicks /
   MaxIntraday / MaxEOD — the samples' 7/5 divergence proves per-account config).
2. **Expired-contract history servability** (expired contracts are LISTABLE per the
   prior note; whether their history is SERVED is unverified).
3. **History == recorded-live equivalence** (does `GetHistory` `TICK` replay exactly
   what `SubscribeRealtime` streamed? — the core integrity probe).
4. **TLS support** (wss:// / https:// acceptance; examples are plaintext ws://).
5. **Real rate/throttle behavior** (no rate card; SDKs carry no throttle code; only
   `AllowedCallsPerHour/Month` fields, `-1` in samples).
6. **Sustained WS uptime + gap profile** (multi-day soak; the SDKs ship no
   heartbeat/reconnect logic, so disconnect frequency is load-bearing).
7. **Actual per-segment depth** (NSE/NFO/BSE/MCX × TICK/MINUTE/EOD measured, not
   inferred from MaxTicks).
8. **GetHistoryAfterMarket semantics** (till-previous-working-day claim; when does
   "today" become servable; cost-saving claim).
9. **Instrument-master completeness vs the Groww master** (symbol coverage,
   identifier mapping, expired-contract listing depth).
10. **Contractual licensing/redistribution terms** (storage rights, single-
    subscriber scope, S3/re-publication, refund terms — ask sales; nothing public).

## Honest limits (what remains unverified, and exactly what would verify each)

- **Depth numbers** (7 days / 3 months / 2010) are unconfirmed — verified by the
  operator uploading the official type-of-data-available page (`MANIFEST.md` upload
  list, item 1) AND probe gates 1+7 with a real key.
- **Heartbeat-vs-change-only row emission** is unmeasured — verified by re-running
  the sample-CSV measurement counting identical-consecutive rows, or a short live
  capture on a quiet instrument.
- **REST vs WS `LastTradeTime` epoch unit divergence** (REST samples show
  milliseconds, WS samples show seconds) — verified by one live GetHistory call on
  each transport.
- **TLS, rate limits, session policy, history==live equivalence, expired-contract
  servability** — probe gates 3–6 + 2 above; each needs credentials.
- **Licensing/redistribution** — verified only by the subscriber agreement from
  GDFL sales (upload-list item 5); until then the SUMMARIZE-ONLY posture stands.
- The cadence statistics are from **one trading day, five NFO instruments** — a
  narrow (but real) sample; segment-wide generalization needs more days/segments.
- Everything sourced from the PyPI-published references may LAG the live site docs
  (the site itself was unreachable); the operator upload list exists to close that.

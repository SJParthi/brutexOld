# TrueData live WebSocket API — own-words summary

> **Compiled:** 2026-07-20; evidence tiers per `../README.md`. Source of every fact:
> the official `truedata` 7.0.1 / `truedata-ws` 5.0.11 SDK wheels read this session
> (sha256-pinned in `../MANIFEST.md`), the official-author sample repo
> (`Nahas-N/Truedata-sample-code`, via GitHub code search), the in-repo 2026-07-15
> official-page capture (`docs/broker-api-capture/truedata/`), and the #1966 pack
> (`docs/truedata/api-reference/03-websocket-realtime.md` — the deeper wire-order
> reference; consult it for per-field array positions). Summarized in our own words;
> message-type/field/host tokens are vendor identifiers kept verbatim.

## Endpoints, hosts, ports

| Service | Endpoint | Tier |
|---|---|---|
| Live WS (current SDK default) | `wss://push.truedata.in:8084?user=<USER>&password=<PASSWORD>` | SDK-VERIFIED (`truedata/websocket/TD_live.py:14,63`) |
| Live WS (legacy default) | same host, port **8082** | SDK-VERIFIED (`truedata_ws/websocket/defaults.py`); the README notes another live port applies only "if you have been authorized" on it — the live port is **ACCOUNT-ASSIGNED** |
| Migration port | 8083 | PRIOR-VERIFIED-NOTE (2026-07-16 catalog of the PR #1595 branch docs; Telegram announcement) |
| Full Market Feed | same host, port **9084**, `full_feed=True` — whole-exchange-segment snapshot stream, no `addsymbol` needed | SDK-VERIFIED(sample) (official `full_feed_sample_code.py`) + OFFICIAL-VERIFIED(capture-2026-07-15) product grid |
| Replay feed | `wss://replay.truedata.in:<port>` (post-market/weekend replay of the full feed; same client code, swap host / `replay=1`) | OFFICIAL-VERIFIED(capture-2026-07-15) KB + PRIOR-VERIFIED-NOTE (#1966 high) + COMMUNITY corroboration; replay SCHEDULE UNVERIFIED |
| Sandbox | `wstest.truedata.in`, RT port **8086** (its panel also shows 8082) | OFFICIAL-VERIFIED(capture-2026-07-15) + COMMUNITY |
| Beta host | `pushbeta.truedata.in:8088` | OFFICIAL-VERIFIED(capture-2026-07-15) (official callback sample) |
| Corporate-data WS | `wss://corp.truedata.in:9092` | PRIOR-VERIFIED-NOTE (#1966 pkg-source of official `truedata-nodejs`) |

Port→plan/feature map beyond the above: **UNVERIFIED** (account-shaped; probe gate).

## Auth + connect flow

- TLS WebSocket (`wss://`); **credentials ride the URL query string**
  (`?user=<USER>&password=<PASSWORD>`) — no token on the WS. SDK-VERIFIED
  (`TD_live.py:63`). Secret-hygiene note: the password travels in URLs — any
  consumer must choke-point-redact logged URLs (the #1966 pack's own caution).
- On connect the server sends a welcome message; the Node SDK gates subscribes on
  the `'TrueData Real Time Data Service'` welcome. PRIOR-VERIFIED-NOTE (#1966 high).
- **Connect-time entitlement announce:** the welcome carries the plan envelope —
  `subscription` (e.g. `tick+1min`), `segments`, `maxsymbols`, `validity` — i.e.
  the client learns its account entitlements from the socket itself. SDK-VERIFIED
  (`TD_ws.py:96-99`; compressed "A" message fields `maxsymbols`/`subscription`/
  `validity` in `compression_map.py`).
- Feed composition is a **server-side entitlement**: ticks enabled by default;
  1-min bars, 5-min bars, tick+bar combos and Greeks streaming need backend
  enablement; "data that is not enabled by default may require exchange approvals".
  SDK-VERIFIED (official README in `truedata` METADATA — OSS-licensed text).

## Subscription verbs

- `{"method":"addsymbol","symbols":[...]}` / `{"method":"removesymbol",...}` /
  `{"method":"getmarketstatus"}`. SDK-VERIFIED (`TD_live.py:86-98`).
- The Node SDK batches `addsymbol` at **1500 symbols/frame** ("max 65000
  characters") — large plans exist. PRIOR-VERIFIED-NOTE (#1966 high, Node source).
- Acks: `'symbols added'` / `'symbols removed'` with counts + a touchline array.
  PRIOR-VERIFIED-NOTE (#1966 high). Market-status value vocabulary UNVERIFIED.
- No per-message request limit on the live WS; verbs count only against the plan's
  symbol limit. OFFICIAL-VERIFIED(capture-2026-07-15) KB.

## Message families (wire shapes)

All SDK-VERIFIED (array→object mapping in `TD_ws.py` / `utils.py` /
`compression_map.py`; per-position wire order in the #1966 pack `03 §4`) unless
tiered otherwise:

| Family | Shape | Notes |
|---|---|---|
| `touchline` | 18 fields | snapshot per symbol on subscribe (+ post-market updates): Symbol, symbol_id, LastUpdateTime, LTP, LTQ, ATP, TTQ, O/H/L, Prev_Close, OI, Prev_OI_Close, Turnover, Bid/BidQty/Ask/AskQty |
| `trade` (the "tick") | 19 fields | SymbolID, Timestamp, LTP, LTQ, ATP, TTQ, day O/H/L, Prev_Close, OI, Prev_OI_Close, Day_Turnover, **Special tag** (`"OHL"`/`"H"`/`"L"`/`""` — new day open/high/low), **`tick_seq`** (per-symbol sequence, drop detection), Bid/BidQty/Ask/AskQty (bid/ask tail present only when the bid-ask feed is enabled; zeros/absent otherwise — len guard in SDK) |
| `bidask` (L1) | 6 wire fields | one best bid + one best ask; NSE & MCX; "part of the feed by default" (that default claim is capture/KB tier); can be account-disabled to cut volume |
| `bidaskL2` (**BSE only**) | 35 elements | SymbolId, Time, SequenceNo, then **5 levels** × (Bid, BidQty, BidNo, Ask, AskQty, BidNo/AskNo = order-counts), then TotalBidQty/TotalAskQty. The BSE-only depth exception |
| `bar1min` / `bar5min` | 8 fields | SymbolId, Time, O, H, L, C, Volume, OI — server-built COMPLETED bars, delivered for all subscribed symbols within ~1 s of bar completion (capture KB); bars are labeled by bucket-OPEN minute (09:15:00–09:15:59 → bar 09:15 — matches BruteX's resampler convention). Streaming bars need backend enablement |
| `greeks` | 8 fields | SymbolId, Time, IV, Delta, Theta, Gamma, Vega, Rho — a separate opt-in add-on stream ("needs backend enablement"); computed-by-whom UNVERIFIED |
| full-feed bars | `{interval, data:[[Time, SymbolId, O,H,L,C,Volume,OI],…]}` | Time/SymbolId order SWAPPED vs single-symbol bars (a real parser trap) |
| `marketstatus` / `HeartBeat` | status + timestamp frames | see below |

- **Compression:** since `truedata` v7.0.0 all WS messages are LZ4-compressed by
  default (`compression=False` ctor opt-out; binary field maps in
  `compression_map.py`; v7.0.1 is a compression bugfix). SDK-VERIFIED.
- **Timestamps:** every data-message timestamp parses `'%Y-%m-%dT%H:%M:%S'`; the
  compressed protocol carries 4-byte epoch SECONDS. **No sub-second field exists
  anywhere on this wire.** SDK-VERIFIED — the load-bearing fact for the cadence
  verdict below.

## Symbols + segments on the live socket

- Segments: NSE EQ / Indices / F&O / CDS, BSE EQ / Indices / F&O, MCX —
  OFFICIAL-VERIFIED(capture-2026-07-15) signup/segment lists + PRIOR-VERIFIED-NOTE
  (2026-07-16 catalog). Segment access is exchange-approval/compliance-gated per
  account (SDK-VERIFIED README note).
- Index symbols use exchange spot names WITH SPACES (`NIFTY 50`, `NIFTY BANK`,
  `INDIA VIX`) — PRIOR-VERIFIED-NOTE (2026-07-16 catalog; the exact `INDIA VIX`
  string + its live-feed servability are flagged for the symbol-master download +
  probe gate 1). Options: `<ticker><YYMMDD><strike><CE|PE>` (weekly + monthly same
  format); continuous futures `SYMBOL-I/-II/-III`; BSE equities carry the `_BSE`
  suffix — PRIOR-VERIFIED-NOTE.
- Per-account gating is explicit vendor policy — subscriptions are streamlined at
  the vendor backend and only the segments/ports assigned to the account work
  (PRIOR-VERIFIED-NOTE, official Telegram announcement via the 2026-07-16 catalog).

## Heartbeat, reconnect, session policy

- Server heartbeat ~**5 seconds** (`TD_ws.py:25` `heartbeat_interval = 5`;
  corroborated by the capture KB "every 5 seconds"). SDK-VERIFIED + capture.
- Client law (Python SDK): `run_forever(ping_interval=10, ping_timeout=5)`;
  auto-reconnect re-subscribes the symbol set. SDK-VERIFIED (`TD_live.py:71`).
  Node SDK law: close + reconnect if no heartbeat >15 s (checked every 20 s),
  reconnect every 2 s. PRIOR-VERIFIED-NOTE (#1966 high). Known pitfall:
  `websocket-client==0.58.0` broke RT connections (pin 0.57.0) —
  PRIOR-VERIFIED-NOTE (2026-07-16 catalog).
- **One concurrent WS login per user** — "Connect only from 1 place at a time
  (1 login instance)". OFFICIAL-VERIFIED(capture-2026-07-15) KB. A force-logout
  endpoint exists (`api.truedata.in/logoutRequest?...` + ~5-min wait) for stuck
  sessions. OFFICIAL-VERIFIED(capture-2026-07-15).
- Maintenance windows: trading weekdays 07:30–08:00; Sat/Sun 07:30–10:30 IST.
  PRIOR-VERIFIED-NOTE (2026-07-16 catalog, official KB rows).

## THE CADENCE VERDICT (what the socket actually pushes)

Restating `../VERDICT.md` with tiers — this is the page's load-bearing section:

- The vendor's own product grid: "Streaming Live Data API — L1 data @1sec frequency
  with single best bid/ask details" — OFFICIAL-VERIFIED(capture-2026-07-15), the
  only explicit frequency number in the corpus.
- The wire cannot express anything finer: second-granular timestamps everywhere,
  4-byte epoch-seconds in the compressed protocol — SDK-VERIFIED.
- The `trade` record is a NETTED SNAPSHOT (day-cumulative TTQ/ATP/turnover + day
  OHL + OI on every message), not an exchange trade print — SDK-VERIFIED shape.
- `tick_seq` guarantees lossless delivery OF WHAT IS SENT — it does not make the
  stream per-trade. PRIOR-VERIFIED-NOTE (2026-07-16 catalog).
- Emission model: event-on-change reading (SDK event wording + `tick_seq`), so >1
  msg/s per busy symbol is plausible — measured msgs/sec per segment is UNVERIFIED.
- The vendor's own blog distinguishes this class from NSE TBT ("every order or a
  change in the order" — a product it does not sell) —
  OFFICIAL-SNIPPET-UNCONFIRMED.

**Net: a ~1-second-class coalesced L1 snapshot stream. Marketing "tick-by-tick" =
every such snapshot update, NOT NSE order-by-order TBT.**

## Named probe gates (live, credentialed — the WS subset of `../VERDICT.md`)

1. **Per-segment msgs/sec count** — liquid NIFTY future + option, `NIFTY 50`,
   `INDIA VIX`, an MCX future, a BSE symbol, during open; do distinct trades appear
   between snapshots?
2. **`tick_seq` gap inspection** — drops under load; monotonicity per symbol.
3. **Heartbeat-vs-change emission** — does a quiet symbol emit every second anyway
   or skip seconds (and what do index rows carry, having no trade tape)?
4. Timestamp sub-second digits on the raw wire (expected: none).
5. Raw JSON key names, uncompressed + LZ4 (or the gated API PDF v2.2.1).
6. Port→plan assignment law; numeric `maxsymbols` tiers; whether >1 login is
   purchasable.
7. Greeks/bidask stream cadence per symbol; greeks computed-by-whom.
8. Measured delivery latency distribution (no vendor SLA exists to test against).

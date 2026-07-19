# 03 — Real-time WebSocket API (streams, message shapes, replay)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Primary evidence: vendor-published SDK source —
`truedata-nodejs` 1.2.0 (`feed.js`, `utils.js`) + `truedata` 7.0.1
(`TD_live.py`) — pkg-source = high confidence.

## 1. Hosts, ports & connection

| Claim | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Live WS | `wss://push.truedata.in:<port>?user=<u>&password=<p>` | `feed.js` + `TD_live.py` (pkg-source) | high |
| Replay WS | `wss://replay.truedata.in:<port>` (same query auth) — selected with `replay = 1` | `feed.js` (pkg-source); PyPI truedata-ws README lists "Replay Server: replay.truedata.in" (direct-fetch) | high |
| Corporate WS | `wss://corp.truedata.in:9092?user=&password=` (default 9092) | `corporateFeed.js` (pkg-source) | high |
| Default live port (Node SDK) | **8082** (`rtConnect(..., userPort = 8082, ...)`) | `feed.js:42` (pkg-source) | high |
| Default live port (Python SDK v7) | **8084** (`TD_live(..., live_port=8084, ...)`) — newer SDK default differs from the older 8082 | `TD_live.py:14` (pkg-source) | high |
| Older Python SDK | 8082 default; README: "Alternative Port: live_port=8084 (if authorized)" | PyPI truedata-ws README (direct-fetch) | high |
| Full-feed port | vendor sample uses `live_port=9084` with `full_feed=True` | `full_feed_sample_code.py` (github-fetch of the vendor-staff sample repo) | high |
| Port→plan/feature map | ports vary by authorization; the full mapping is NOT published in retrievable sources | — | UNVERIFIED beyond 8082/8084 live + 9084 full-feed + 9092 corporate |
| Compression | Python SDK v7.0.0 "WebSocket message compression enabled" (`compression=False` ctor default; lz4 dependency; v7.0.1 "Compression bugfix") | PyPI changelog (direct-fetch) + `TD_live.py` ctor (pkg-source) | high |

## 2. Commands (client → server) — verbatim (vendor Node source, high)

```js
// feed.js
module.exports.rtConnect = (username, password, allSymbols, userPort = 8082,
        bidaskFeed = 1, heartbeat = 1, replay = 0, url = 'push') => { ... }
if (replay === 1) connectionUrl = 'wss://replay.truedata.in:';
url = connectionUrl + ':' + port + '?user=' + user + '&password=' + pwd;

function subscribe(symbols) {
  //for-loop to override max 65000 characters
  for (let i = 0; i <= symbols.length; i += 1500) {
    const jsonRequest = { method: 'addsymbol', symbols: symbols.slice(i, i + 1500) };
    connection.send(JSON.stringify(jsonRequest));
  }
}
function unSubscribe(symbols) { ... { method: 'removesymbol', symbols: ... } ... }
module.exports.getMarketStatus = () => {
  connection.send(JSON.stringify({ method: 'getmarketstatus' }));
};
```

| Command | Shape | Notes |
|---|---|---|
| Subscribe | `{"method":"addsymbol","symbols":[…]}` | SDK batches 1500 symbols per message ("max 65000 characters") |
| Unsubscribe | `{"method":"removesymbol","symbols":[…]}` | |
| Market status | `{"method":"getmarketstatus"}` → server `marketstatus` message with `data` | exact status-value vocabulary UNVERIFIED |

## 3. Server → client session messages (high)

- Welcome: `success: true`, `message: 'TrueData Real Time Data Service'`
  — the SDK subscribes only after receiving it.
- Subscribe ack: `message: 'symbols added'` with `symbolsadded`,
  `totalsymbolsubscribed`, `symbollist` (a touchline array per symbol);
  unsubscribe ack `'symbols removed'` with `symbolsremoved`,
  `totalsymbolsubscribed`.
- Heartbeat: `message: 'HeartBeat'` with `timestamp`. The Node SDK closes
  + reconnects if no heartbeat for >15 s (checked every 20 s);
  auto-reconnect every 2 s. (Client behavior high; the server's own
  heartbeat cadence is inferred ≤15 s — not stated.)

## 4. Data message shapes (wire ARRAY order decoded from the vendor SDK's array→object mapping)

Data arrives keyed by message type: `trade`, `bidask`, `bidaskL2`
(BSE only), `bar1min`, `bar5min`, `greeks` (+ full-feed variants). All
high confidence (pkg-source), KB-corroborated where noted.

### touchline (snapshot per symbol on subscribe / `message:'touchline'`) — 18 fields
`[0]=Symbol, [1]=symbol id (map key), [2]=LastUpdateTime, [3]=LTP, [4]=TickVolume(LTQ), [5]=ATP, [6]=TotalVolume(TTQ), [7]=Open, [8]=High, [9]=Low, [10]=Previous_Close, [11]=Today_OI, [12]=Previous_Open_Interest_Close, [13]=Turnover, [14]=Bid, [15]=BidQty, [16]=Ask, [17]=AskQty`

### trade (tick)
`[0]=Symbol_ID, [1]=Timestamp, [2]=LTP, [3]=LTQ, [4]=ATP, [5]=Volume(TTQ day), [6]=Open, [7]=High, [8]=Low, [9]=Prev_Close, [10]=OI, [11]=Prev_Open_Int_Close, [12]=Day_Turnover, [13]=Special(tag: "OHL"/"H"/"L"/""), [14]=Tick_Sequence_No, [15]=Bid, [16]=Bid_Qty, [17]=Ask, [18]=Ask_Qty`
— bid/ask fields present only when the bid-ask feed is enabled (the SDK
renders 'Deactivated' otherwise). Client-derived extras: Price_Change,
Price_Change_Percentage, OI_Change, OI_Change_Percentage. The KB tick
field list (search-relay) matches this order exactly.

### bidask (L1)
`[0]=SymbolId, [1]=Time, [2]=Bid, [3]=BidQty, [4]=Ask, [5]=AskQty`
— L1 (single best bid/ask) for NSE & MCX, part of the feed by default
(KB via search-relay, medium).

### bidaskL2 (BSE only) — 35 elements
`[0]=SymbolId, [1]=Time, [2]=SequenceNo, then 5 levels × (Bid, BidQty, BidNo, Ask, AskQty, AskNo), then TotalBidQty, TotalAskQty`

### bar1min / bar5min
`[0]=SymbolId, [1]=Time, [2]=Open, [3]=High, [4]=Low, [5]=Close, [6]=Volume, [7]=OI`
— KB (search-relay): "1 min candle csv includes date, time, open, high,
low, close, volume and OI." Streams: tick-only, tick+1min, or tick+5min
combos; 1-min/5-min streaming "needs backend enablement" (KB + PyPI
truedata-ws README; medium/high).

### greeks
`[0]=SymbolId, [1]=Time, [2]=IV, [3]=Delta, [4]=Theta, [5]=Gamma, [6]=Vega, [7]=Rho`

### full-feed bars
`{interval: <bar>, data: [[Time, SymbolId, O, H, L, C, Volume, OI], …]}`
— note Time/SymbolId order is SWAPPED vs the single-symbol bar shape.

## 5. Python v7 object fields (PyPI description, direct-fetch — vendor-authored, high)

- Live tick object: `timestamp, symbol_id, symbol, ltp, ltq, atp, ttq,
  day_open, day_high, day_low, prev_day_close, oi, prev_day_oi, turnover,
  special_tag, tick_seq, best_bid_price, best_bid_qty, best_ask_price,
  best_ask_qty, change, change_perc, oi_change, oi_change_perc`
- Bar object: `symbol, symbol_id, day_open, day_high, day_low,
  prev_day_close, prev_day_oi, oi, ttq, timestamp, open, high, low,
  close, volume, change, change_perc, oi_change, oi_change_perc`
- Callback decorators: `@trade_callback, @bidask_callback,
  @one_min_bar_callback, @five_min_bar_callback, @greek_callback,
  @full_feed_trade_callback, @full_feed_bar_callback`

## 6. Full market feed

`rtConnectFullFeed` (Node) / `full_feed=True` (Python) — whole-market
push. The SDK bootstraps a symbol-id map from
`api.truedata.in/getAllSymbols?segment=all&…&allexpiry=false` and caches
per-segment prev-close/OI from `getprevcloseandoi` for segments
`EQ, FO, MCX, CDS, IN, BSEEQ, BSEFO`. (pkg-source, high; commercial terms
of the full-feed entitlement UNVERIFIED.)

## 7. Replay service (off-hours development)

- "Replay feed is NOW Available & activated for all Active TrueData
  Market Data API Subscribers" — for daily post-market and weekend
  development, at no additional cost (KB replay articles via
  search-relay, medium).
- Mechanically the same protocol: `replay=1` → `wss://replay.truedata.in:<port>`
  (pkg-source, high). Replay schedule (when it runs, which day is
  replayed): UNVERIFIED.
- **TickVault note:** a vendor-served replay is a genuinely useful
  development harness for a live-capture system (deterministic feed
  without market hours); worth a dedicated probe if TrueData is ever
  trialed.

## 8. Limits touching the WS (details in [07](./07-limits-plans.md))

1 login instance at a time; symbol count is a PLAN attribute
(addsymbol/removesymbol/getmarketstatus/logout operations "combined and
limited according to your Plan's symbols limit") — KB via search-relay,
medium; exact per-plan counts UNVERIFIED.

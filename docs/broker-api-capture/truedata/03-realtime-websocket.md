# TrueData — Real-Time WebSocket Feed (Market Data API)

> Sources:
> - https://feedback.truedata.in/knowledge-base/article/fields-included-in-real-time-tick-1-min-bar-streaming (official field list)
> - https://feedback.truedata.in/knowledge-base/article/truedata-market-data-api-how-are-the-bars-formed-is-the-data-delayed
> - https://feedback.truedata.in/knowledge-base/article/full-market-feed-replay-is-now-live-market-data-api_1
> - https://feedback.truedata.in/knowledge-base/article/want-to-200-symbol-historical-data-in-one-go-like-live-streaming
> - https://feedback.truedata.in/knowledge-base/article/how-to-fetch-real-time-data-for-the-market-data-api-on-the-truedata-sandbox-page
> - https://pypi.org/project/truedata/ , https://pypi.org/project/truedata-ws/ (official field dataclasses)
> - https://www.npmjs.com/package/truedata-nodejs (event names)
> - https://www.nuget.org/packages/TrueData-DotNet/ (raw JSON message keys)
> - https://wstest.truedata.in/ (sandbox client)

## 1. Connection

```
wss://push.truedata.in:8082?user=<USER_ID>&password=<PASSWORD>
```

Replay: `wss://replay.truedata.in:8082` (same credentials/port; see §8). Alternate assigned live ports: 8084; beta host `pushbeta.truedata.in:8088`; full-feed port 9084 (see §9). Only one concurrent login per user.

After connect, the server sends a welcome/handshake message whose JSON contains the text `TrueData` (the official .NET sample subscribes upon `e.JsonMsg.Contains("TrueData")`).

## 2. Subscription management

The feed is subscription-based: you add ("subscribe") and remove ("unsubscribe") symbols on the open socket. The sandbox UI exposes exactly these operations — Connect / Disconnect / **Add NSE Symbols / Add BSE Symbols / Add MCX Symbols / Add CDS Symbols / Remove Symbols** — each of which sends a JSON request text over the socket ("Request:" box + "Send Text" button).

Operations counted against your plan (KB Request/Call limit article, verbatim): `addSymbol / removeSymbol / getMarketStatus / logout - all combined - As per your Plan's symbols limit`.

So the WS request methods are:

| Method | Purpose | SDK equivalents |
|---|---|---|
| add symbol(s) | Subscribe symbols (up to plan's symbol limit) | Python `start_live_data([symbols])`; Node `rtSubscribe(newSymbols)` / initial list in `rtConnect(...)`; .NET `tDWebSocket.Subscribe(string[] symbols)` |
| remove symbol(s) | Unsubscribe | Python `stop_live_data([symbols])`; Node `rtUnsubscribe(oldSymbols)` |
| get market status | Query market open/close state | Node `getMarketStatus()` |
| logout / disconnect | Close session | Python `disconnect()`; Node `rtDisconnect()` |

On successful add, the server returns the **touchline** (snapshot) for each added symbol, then streams updates. Symbols once added continue to flow in a streaming manner with **no request limit on the real-time API**.

Example subscribe symbol array (official .NET sample):

```csharp
string[] symbols = { "NIFTY-I", "RELIANCE", "NIFTY 50", "CRUDEOIL-I", "USDINR-I", "SENSEX", "RELIANCE_BSE" };
tDWebSocket.Subscribe(symbols);
```

(Note the `_BSE` suffix for BSE-listed equity and plain index names like `NIFTY 50` / `SENSEX`.)

## 3. Message types on the wire

Raw messages are JSON. The official .NET sample dispatches on these JSON markers:

| JSON marker | Meaning |
|---|---|
| contains `"TrueData"` | Connect acknowledgement / welcome |
| contains `"trade"` | Trade tick message (`SnapData`: SymbolId, Timestamp, LTP, Volume, Open, High, Low, PrevClose, …) |
| contains `"bidask"` | Bid/Ask message (`BidAsk`: SymbolId, Timestamp, Bid, BidQty, Ask, …) |
| contains `"HeartBeat"` | Heartbeat (`hb.timestamp`, `hb.message`) — every 5 seconds |
| (bar messages) | 1-min / 5-min bar data (Node event `bar` receives both) |
| (market status) | Market open/close status messages (Node event `marketstatus`) |
| (greeks) | Option greeks stream (Node event `greeks`) |
| (touchline) | Snapshot on subscribe (Node event `touchline`) |

Node.js event names (official README — the complete set of stream types):

```js
rtFeed.on('touchline', touchlineHandler);   // Touchline data (snapshot after subscribe)
rtFeed.on('tick', tickHandler);             // Tick data
rtFeed.on('greeks', greeksHandler);         // Greeks data
rtFeed.on('bidask', bidaskHandler);         // Bid Ask data (if enabled)
rtFeed.on('bidaskL2', bidaskL2Handler);     // Level 2 Bid Ask (BSE exchange only)
rtFeed.on('bar', barHandler);               // 1min and 5min bar data
rtFeed.on('marketstatus', marketStatusHandler); // market status messages
rtFeed.on('heartbeat', heartbeatHandler);   // heartbeat message and time
```

### Heartbeats & market status

- Heartbeats arrive **every 5 seconds**; TrueData recommends logging them to verify a steady connection (Python: visible at `log_level=logging.DEBUG`).
- DEBUG level also shows: Market Status messages (market open/close as they happen), automatic touchline update messages, symbol add/remove messages.

## 4. Feed combinations (server-side entitlements)

Streaming composition is set per account on TrueData's backend (may require exchange approvals):

- Live data (Streaming Ticks) — **enabled by default**
- Live Data (Streaming 1 min bars)
- Live Data (Streaming 5 min bars)
- Live data (Streaming Ticks + 1 min bars)
- Live Data (Streaming Ticks + 5 min bars)
- Live Data (Streaming Ticks + 1 min bars + 5 min bars)
- Live Data (Streaming 1 min + 5 min bars)
- Option Greek streaming — needs backend enablement
- Bid/Ask: NSE & MCX L1 best bid/ask is part of the feed by default; **you can opt out** of bid/ask to reduce feed volume. BSE offers L2 (top 5).

## 5. Tick (trade) message — field order & meaning

Official KB (verbatim): "Below are the fields included in Real-Time Tick Streaming with TrueData Market API (WebSocket):"

```
Format >> Symbol ID, Date-Time (Timestamp), LTP, LTQ, ATP (Average Traded Price – for the day),
TTQ (Total Traded Qty – Volume for the day), Open, High, Low, Prev Close, OI (Open Interest),
Prev Open Int Close, Day's Turnover, Special Tag ("OHL", "H", "L" or ""), Tick Sequence No,
Bid, Bid Qty, Ask, Ask Qty
```

| # | Field | Description |
|---|---|---|
| 1 | Symbol ID | TrueData numeric symbol id |
| 2 | Date-Time | Timestamp of tick |
| 3 | LTP | Last traded price |
| 4 | LTQ | Last traded quantity |
| 5 | ATP | Average traded price (day) |
| 6 | TTQ | Total traded qty (day volume) |
| 7 | Open | Day open |
| 8 | High | Day high |
| 9 | Low | Day low |
| 10 | Prev Close | Previous day close |
| 11 | OI | Open interest |
| 12 | Prev Open Int Close | Previous day OI close |
| 13 | Day's Turnover | Turnover for the day |
| 14 | Special Tag | `"OHL"`, `"H"`, `"L"` or `""` (tick made new day Open/High/Low) |
| 15 | Tick Sequence No | Sequence number |
| 16 | Bid | Best bid price |
| 17 | Bid Qty | Best bid quantity |
| 18 | Ask | Best ask price |
| 19 | Ask Qty | Best ask quantity |

Python dataclass view of the same tick (`td_obj.live_data[symbol]` fields, official README):

```
["timestamp","symbol_id","symbol","ltp","ltq","atp","ttq","day_open","day_high","day_low",
 "prev_day_close","oi","prev_day_oi","turnover","special_tag","tick_seq",
 "best_bid_price","best_bid_qty","best_ask_price","best_ask_qty",
 "change","change_perc","oi_change","oi_change_perc"]
```

(`change*`/`oi_change*` are computed by the library. If Bid/Ask is not enabled for the account, zeros are appended in the bid/ask fields "to keep the structure intact" — truedata-ws 5.0.2 note. Python tick objects expose `tick_type` where `1` = trade packet vs bid-ask-only packet, and `.to_dict()`.)

## 6. Bar messages (1-min & 5-min streaming)

Official KB (verbatim): "Below are the fields included in Real-Time 1 Minute Bar Data Streaming":

```
Format >> Symbol ID, Date-Time (Timestamp), bar_open, bar_high, bar_low, bar_close (LTP), bar volume, OI
```

Python dataclass view (`one_min_live_data` / `five_min_live_data` fields):

```
["symbol","symbol_id","day_open","day_high","day_low","prev_day_close","prev_day_oi","oi","ttq",
 "timestamp","open","high","low","close","volume","change","change_perc","oi_change","oi_change_perc"]
```

### How bars are formed / is data delayed? (KB, verbatim)

```
For data coming from:
09:15:00 to 09:15:59 > bar 09:15
09:16:00 to 09:16:59 > bar 09:16
09:17:00 to 09:17:59 > bar 09:17
...
15:28:00 to 15:28:59 > bar 15:28
15:29:00 to 15:29:59 > bar 15:29
15:30:00 to 15:30:59 > bar 15:30 (but the market closes at 15:30:00, so there can be max 1 tick
                                  or even no tick in the last second resulting in no 15:30 bar)
```

A bar made of a single tick has open=high=low=close (explains "at 3:30 PM OHLC == LTP"). Bar-streaming delivers the completed bar for **all subscribed symbols simultaneously within 1 second of bar completion** — usable as a pseudo-bulk quote mechanism for 200+ symbols (KB "Need 200 Symbol Historical data in one go").

## 7. Bid/Ask & Level-2 messages

- NSE (and MCX): **Level 1** — one best bid & ask. `bidask_data.bid`/`.ask` = list of tuples `[(price, qty)]` (length 1).
- BSE: **Level 2** — top 5. `[(price_1, qty_1, no_of_trades_1), (price_2, qty_2, no_of_trades_2), ...]` (length 5 on both bid & ask). Node event: `bidaskL2` ("only for BSE exchange"). BSEFO L2 support added in truedata-ws 5.0.1.
- Python bidask dataclass fields: `["timestamp","symbol_id","symbol","bid","ask","total_bid","total_ask"]` (`total_bid`/`total_ask` added in truedata-ws 5.0.10 for L2).

## 8. Touchline

On subscribe (and automatically post-market), the server sends touchline data — last/settlement snapshot with all day fields. "The touchline data is useful post market hours as this provides the last updates / settlement updates / snap quotes … during market hours all of the fields of the touchline data already form part of the real time market data feed." Python: `td_obj.touchline_data[req_id]` (populate ~1s after subscribe).

## 9. Option greeks stream

If subscribed to NSE options + greeks add-on, greek messages stream per option symbol. Fields (official):

```
["timestamp","symbol_id","symbol","iv","delta","theta","gamma","vega","rho"]
```

## 10. Market Replay feed (post-market development)

KB (verbatim key facts):

- "Replay feed is NOW Available & activated for all Active TrueData Market Data API Subscribers."
- `url = 'replay.truedata.in'`, `realtime_port = 8082` — "Replace the url push.truedata.in to replay.truedata.in. Port remains the same - 8082."
- Python: `td_obj = TD(username, password, live_port=8082, url='replay.truedata.in')`
- C#: `tDWebSocket = new TDWebSocket("user_id", "user_pwd", "wss://replay.truedata.in", 8082);`
- Node: `rtConnect(user, pwd, symbols, port, bidask = 1, heartbeat = 1, replay = 1);` — `replay > 1 = Feed replay, 0 = Normal feed (default)`
- "All code should work as is without any changes." Available daily post market hours & weekends; no additional cost; for trial + paid users. (Replay session timings are published as an image on the KB page/Telegram.)
- Make sure to switch back to the production URL before live market start.

## 11. Full Market Feed (entire-exchange stream)

Official sample (`full_feed_sample_code.py`):

```python
td_obj = TD_live(username, password, url="push.truedata.in", live_port=9084,
                 log_level=logging.WARNING, full_feed=True, dry_run=False)

@td_obj.full_feed_trade_callback
def full_feed_trade(tick_data): ...

@td_obj.full_feed_bar_callback
def full_feed_bar(bar_data): ...
```

- No `start_live_data()` — the full segment feed streams automatically (subject to subscription: "Realtime Exchange Snapshot API — get live data of entire exchange segment in single call").
- Python callbacks: `@full_feed_trade_callback`, `@full_feed_bar_callback` (bar callback for full feed added in `truedata` package).

## 12. Sandbox testing (wstest.truedata.in)

Real-time (KB steps, verbatim):

1. Visit https://wstest.truedata.in/
2. Step 1 >> Select RT (8086)  *(page also displays "Real Time (Port 8082)")*
3. Step 2 >> Enter your User Name and Password
4. Step 3 >> Click on Add Symbols
5. Step 4 >> In the "Request" column enter symbols of your choice (MCX and/or NSE Futures)
6. Step 5 >> Click "Send Text"
7. Step 6 >> You will see Real-Time Data in the Message Log

The page also embeds the REST history tester (auth.truedata.in login + history.truedata.in query with Symbol / Interval (1min…EOD/Tick) / DelVolume / From / To) rendering a table of `Sr | Timestamp | LTP | O | H | L | C | V | OI`.

## 13. Client-side guidance

- Use threading in your own code when handling multi-symbol feeds (official FAQ answer).
- In Python while-loop mode, always `time.sleep(0.05)`-ish between iterations "otherwise cpu will overthrottle".
- Callbacks execute on the websocket thread — keep them light; don't block with indefinite loops.

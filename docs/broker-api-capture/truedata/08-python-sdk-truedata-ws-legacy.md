# TrueData — Legacy Python SDK: `truedata-ws` (v5.0.11, final)

> Source: https://pypi.org/project/truedata-ws/ (fetched 2026-07-15; release 5.0.11, Nov 16 2023; author Kapil Marwaha <kapil@truedata.in>; GPLv3; Python >= 3.7; homepage https://github.com/kapilmar/truedata-ws)
>
> Superseded by the `truedata` package (07-python-sdk-truedata.md) but still widely deployed; documented here in full because its README is the most complete official Python reference (incl. strategies & full release history).

## Coverage

**WebSocket APIs** — same matrix as the new package (ticks default; 1min/5min bars, tick+bar combos, Option Greek streaming need backend enablement).
**REST APIs** — Historical Data.

## Installation

```
python3 -m pip install truedata_ws
# or
pip3 install truedata_ws
```

Minimum: Python >= 3.7. Dependencies: `websocket-client>=0.57.0`, `colorama>=0.4.3`, `python-dateutil>=2.8.1`, `pandas>=1.0.3`, `setuptools>=50.3.2`, `requests>=2.25.0`, `lz4==3.1.3` (pinned; ">3.1.3 currently have some errors").

## Connecting / logging in

```python
from truedata_ws.websocket.TD import TD
td_obj = TD('<enter_your_login_id>', '<enter_your_password>')
# Connects to default real-time port 8082 & the REST History feed.
# If authorized on another live port:
# td_obj = TD('<id>', '<pwd>', live_port=8084)

# Historical only:
td_obj = TD('<id>', '<pwd>', live_port=None)

# Real-time only:
td_obj = TD('<id>', '<pwd>', historical_api=False)
```

Automatic reconnection: on WS disconnect the lib waits for stable internet, reconnects, **auto re-subscribes symbols** and restarts live data seamlessly.

## Logging

```python
td_obj = TD('<id>', '<pwd>', live_port=realtime_port, url=url,
            log_level=logging.WARNING, log_format="%(message)s")
# or "(%(asctime)s) %(levelname)s :: %(message)s (PID:%(process)d Thread:%(thread)d)"
# DEBUG shows: market status msgs, touchline updates, symbol add/remove, heartbeats (5s)
td_obj = TD('<id>', '<pwd>', log_handler=log_handler_obj)  # custom handler
```

## Real-time data

```python
req_ids = td_obj.start_live_data(['CRUDEOIL-I', 'BANKNIFTY-I', 'RELIANCE', 'ITC'])
# returns list of request ids used to reference data later

import time; time.sleep(1)          # wait ~1s for touchline to populate
for req_id in req_ids:
    print(td_obj.touchline_data[req_id])

last_traded_price     = td_obj.live_data[req_id].ltp
change_perc           = td_obj.live_data[req_id].change_perc
day_high              = td_obj.live_data[req_id].day_high
highest_available_bid = td_obj.live_data[req_id].best_bid_price
```

**VERY IMPORTANT (tick & bar simultaneously):** with `Tick only` or `Bar only` subscription use `td_obj.live_data[req_id]`. If subscribed to both tick & bar: tick → `live_data[req_id]`, 1-min bars → `one_min_live_data[req_id]`, 5-min bars → `five_min_live_data[req_id]`.

```python
td_obj.stop_live_data(['<symbol_1>', ...])
td_obj.disconnect()
```

Custom request-id helper: `symbol_ids = td_obj.get_req_id_list(2000, len(symbols))` (= `list(range(2000, 2000+len(symbols)))`), pass via `start_live_data(symbols, req_id=symbol_ids)`.

## Greeks / BidAsk callbacks

```python
@td_obj.greek_callback
def mygreek_callback(greek_data): print("Greek > ", greek_data)
# greek_data fields: timestamp, symbol_id, symbol, iv, delta, gamma, theta, vega, rho

@td_obj.bidask_callback
def mybidask_callback(bidask_data): print("BidAsk > ", bidask_data)
# bidask_data fields: timestamp, symbol_id, symbol, bid, ask
# NSE = L1 [(ask, ask_qnty)]; BSE = L2 [(ask_1, ask_1_qnty, ask_1_no_of_trades), ... x5]
```

## Quick start (callbacks — recommended)

```python
from truedata_ws.websocket.TD import TD
import time, logging

username = 'your_username'; password = 'your_password'
realtime_port = 8082
url = 'push.truedata.in'
# url = 'replay.truedata.in'   # for the replay feed (re-enable production url before market)

symbols = ['<symbol_1>', '<symbol_2>', ...]
td_obj = TD(username, password, live_port=realtime_port, url=url,
            log_level=logging.DEBUG, log_format="%(message)s")
req_ids = td_obj.start_live_data(symbols)
time.sleep(1)  # very important - touchline population
for req_id in req_ids:
    print(f'touchlinedata -> {td_obj.touchline_data[req_id]}')

@td_obj.trade_callback
def mytrade_callback(tick_data): print("Tick > ", tick_data)

@td_obj.bidask_callback
def mybidask_callback(bidask_data): print("BidAsk > ", bidask_data)

@td_obj.full_feed_trade_callback
def myfullfeed_callback(tick_data): print("Tick > ", tick_data)

@td_obj.greek_callback
def mygreek_callback(greek_data): print("Greek > ", greek_data)

@td_obj.one_min_bar_callback
def my_onemin_bar(symbol_id, tick_data): print("one min > ", tick_data)

@td_obj.five_min_bar_callback
def my_five_min_bar(symbol_id, tick_data): print("five min > ", tick_data)

while True:
    time.sleep(120)   # keep thread alive
```

(Callbacks are dataclass-based since 5.0.1; each supports dot access & `.to_dict()`. Tick callbacks run on the websocket thread — keep them light.)

## Quick start (while-loop / state-check)

```python
req_ids = td_obj.start_live_data(symbols)
subs = td_obj.live_websocket.subs           # active subscription types: 'tick' | '1min' | '5min'
live, one_min, five_min = {}, {}, {}
time.sleep(3)
if 'tick' in subs:
    for r in req_ids: live[r] = deepcopy(td_obj.live_data[r])
if '1min' in subs:
    for r in req_ids: one_min[r] = deepcopy(td_obj.one_min_live_data[r])
if '5min' in subs:
    for r in req_ids: five_min[r] = deepcopy(td_obj.five_min_live_data[r])

while True:
    for r in req_ids:
        if 'tick' in subs and td_obj.live_data[r] != live[r]:
            live[r] = deepcopy(td_obj.live_data[r]);            print('tick ->', td_obj.live_data[r])
        if '1min' in subs and td_obj.one_min_live_data[r] != one_min[r]:
            one_min[r] = deepcopy(td_obj.one_min_live_data[r]); print('one min ->', td_obj.one_min_live_data[r])
        if '5min' in subs and td_obj.five_min_live_data[r] != five_min[r]:
            five_min[r] = deepcopy(td_obj.five_min_live_data[r]); print('five min ->', td_obj.five_min_live_data[r])
    time.sleep(0.05)   # important otherwise cpu will overthrottle
```

## Converting the stream to dict / pandas

```python
td_obj.live_data[req_id].to_dict()
# tick_type == 1 → trade packet (vs bid-ask-only packet)
op_dict = deepcopy(td_obj.live_data[req_id].to_dict())
temp_df = pd.DataFrame.from_records([op_dict])
df = df.append(temp_df, ignore_index=True)
```

## Option chain streaming

```python
nifty_chain  = td_obj.start_option_chain('NIFTY', dt(2021, 8, 26))
sensex_chain = td_obj.start_option_chain("SENSEX", dt(2023, 9, 1), chain_length=80, bse_option=True)
df = nifty_chain.get_option_chain()
nifty_chain.stop_option_chain()
```

Args: `symbol`, `expiry` (date obj), `chain_length` (default 10), `bid_ask` (default False), `market_open_post_hours` (default False; True for e.g. Muhurat session), `bse_option=True` for SENSEX/BANKEX. Chain fields: `symbols, strike, type (CE/PE), ltp, ltt, ltq, volume, price change, price change %, oi, oi Change, OI Change %, bid, bid qty, ask, ask qty`.

Combined chains + live data example (from README): start live data for `['BANKNIFTY-I','NIFTY 50','NIFTY21072915600CE','SBIN']`, then `start_option_chain` for SBIN / BANKNIFTY (chain_length=20) / NIFTY (chain_length=10, bid_ask=True), print chains every 50 ticks, `time.sleep(0.05)` per loop.

## Historical data (REST)

```python
hist_data_1 = td_obj.get_historic_data('BANKNIFTY-I')                     # 1-min bars, today
hist_data_2 = td_obj.get_historic_data('BANKNIFTY-I', duration='3 D')
hist_data_3 = td_obj.get_historic_data('BANKNIFTY-I', bar_size='30 mins')
hist_data_4 = td_obj.get_historic_data('BANKNIFTY-I', start_time=datetime.now()-relativedelta(days=3))
hist_data_5 = td_obj.get_historic_data('BANKNIFTY-I', end_time=datetime(2021, 3, 5, 12, 30))
hist_data_6 = td_app.get_n_historical_bars(symbol, no_of_bars=30, bar_size=barsize)  # Tick, 1 min & EOD
hist_data_7 = td_obj.get_historic_data(symbol, duration='1 D', bar_size='tick', bidask=True)  # default bidask=False
hist_data_8 = td_obj.get_historic_data('BANKNIFTY-I', duration='3 D', bar_size='15 mins')

# storing for later (v4.0.1+; explicit ticker_id required)
td_obj.get_historic_data('BANKNIFTY-I', ticker_id=1000)
print(td_obj.hist_data[1000])

df = pd.DataFrame(hist_data_1)   # single-line pandas conversion
```

Defaults: `end_time=datetime.now()`, `duration="1 D"`, `bar_size="1 min"`. Bar sizes & duration units identical to the new package (see 07). Historical Bid-Ask requires subscription (not active by default).

### get_bhavcopy

```python
eq_bhav  = td_obj.get_bhavcopy('EQ')
fo_bhav  = td_obj.get_bhavcopy('FO')
mcx_bhav = td_obj.get_bhavcopy('MCX')
specific_bhav = td_obj.get_bhavcopy('EQ', date=datetime.datetime(2021, 3, 19))
# "No complete bhavcopy found for requested date. Last available for 2021-03-19 16:46:00."
```

### Gainers / losers

```python
gainers = td_obj.get_gainers(segment="NSEEQ", topn=10, df_style=False)
losers  = td_obj.get_losers(segment="NSEEQ", topn=10)
# segment: NSEEQ, NSEFUT, NSEOPT, MCX ; topn default 10 ; df_style default True
```

## Example strategies (README ships 4)

Differences: callbacks are lightweight/quick-POC and run on the websocket thread (harder to spread across cores); state-check loops poll object changes.

- **Strategy 1** — Tick, callback: BUY when 50-tick SMA > 100-tick SMA, SELL when below. Seeds `data[symbol_id]` from `get_historic_data(symbol, duration='1 D', bar_size='tick')` (last 100 `ltp`), uses `@td_obj.trade_callback`; graceful exit via `td_obj.clear_bidask_callback(); td_obj.stop_live_data(symbols); td_obj.disconnect()`.
- **Strategy 2** — Tick, state check: same SMA logic in `while` loop comparing `live_data` deepcopies, `time.sleep(0.01)`.
- **Strategy 3** — 1-min bars, callback: seeds last 100 closes from `bar_size='1 min'` history, uses `@td_obj.bar_callback` with `min_data.close/symbol/ltp`; exit via `td_obj.clear_bar_callback()`.
- **Strategy 4** — 1-min bars, state check: polls `one_min_live_data` deepcopies with `time.sleep(0.1)`; note "If you are subscribed ONLY to min data, just USE live_data NOT min_live_data".

## Release notes (complete, verbatim highlights)

- **5.0.11** — Bugfix: option chain not updating after connection drop
- **5.0.10** — Bidask L2 total bid/total ask added; td analytics added
- **5.0.8** — Bug fix: annoying behaviour of historical login
- **5.0.7** — reconnection re-subscribes previously subscribed symbols; greeks order fixed
- **5.0.6** — path issue fixed for linux environment
- **5.0.3** — feed optimized with symbolid mapping
- **5.0.2** — if BidAsk not enabled, zeros appended in bid/ask fields of trade ticks to keep structure intact
- **5.0.1** — BidAsk Level 2 (Top 5) for BSEFO; bidask structure changed; bidask callback renamed & dataclass; tick callback dataclass; greeks callback introduced (IV & Greeks); lib optimized
- **4.3.5** — Index segment masters IND → IN
- **4.3.3** — WS reconnection handling improved; Delivery data parameter added to EOD data
- **4.3.2** — `week` & `month` bar sizes; large-dataset streaming optimized
- **4.3.1** — `min_live_data` → `one_min_live_data`; `five_min_live_data` added; bar callbacks renamed to `one_min_bar_callback` / `five_min_bar_callback`; market status at DEBUG; option chain works for all subscriptions
- **4.2.3** — option chain streaming optimized; LTQ/price-change/% added; **rate limiting handled gracefully**
- **4.2.2** — OI-null first ticks error fixed
- **4.2.1** — LZ4 decompression for history downloads; `lz4==3.1.3` pinned
- **4.0.8** — gainers/losers functions; currency option chains; auto-reconnect network bugfix
- **4.0.7** — chains include Previous OI/OI change/volume; `market_close` → `market_open_post_hours`; 32-bit float fix
- **4.0.6** — option chains into pandas; chains work tick & 1-min, pre/post market via touchline; get_n_historical_bars (Tick/1min/EOD)
- **4.0.1** — BREAKING: `hist_data` only stored when `ticker_id` given; tick+min dual access; log handler
- **3.0.2** — auto WS reconnect + auto re-subscribe; duplicate-symbol call avoidance
- **3.0.1** — **Historical via WebSocket deprecated → REST added**; more timeframes; get_bhavcopy; dependency cleanup
- **0.3.11** — `query_time` → `end_time`; `truedata_id` → `symbol_id`; fill missing live_data values

Full release ladder: 0.1.15 (Apr 20 2020) … 1.0.0/1.0.1 (Aug 19 2020), 2.0.x (Jan–Feb 2021), 3.0.1 (Mar 21 2021), 3.0.2 (Apr 21 2021), 4.0.1 (Jun 11 2021) … 4.3.4 (Jul 12 2022), 5.0.11 (Nov 16 2023, final).

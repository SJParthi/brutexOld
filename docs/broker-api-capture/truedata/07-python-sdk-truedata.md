# TrueData — Official Python SDK: `truedata` (current package, v7.0.1)

> Source: https://pypi.org/project/truedata/ (fetched 2026-07-15; release 7.0.1, Jul 14 2025; author Nahas N <nahas@truedata.in>; license GPLv3; requires Python >= 3.10; homepage https://github.com/kapilmar/truedata-ws)
> Official samples: https://github.com/Nahas-N/Truedata-sample-code

The package contains **three modules**: `TD_live` (WebSocket real-time), `TD_hist` (REST history), `TD_analytics` (analytics add-on; see 05-analytics-optionchain-greeks.md).

## Coverage

**WebSocket APIs**
- Live data (Streaming Ticks) — Enabled by Default
- Live Data (Streaming 1 min bars) — Needs backend enablement
- Live Data (Streaming 5 min bars) — Needs backend enablement
- Live data (Streaming Ticks + 1 min bars) — Needs backend enablement
- Live Data (Streaming Ticks + 5 min bars) — Needs backend enablement
- Live Data (Streaming Ticks + 1 min bars + 5 min bars) — Needs backend enablement
- Live Data (Streaming 1 min + 5 min bars) — Needs backend enablement
- Option Greek streaming — Needs backend enablement

(Data not enabled by default may require exchange approvals depending on exchange/segment.)

**REST APIs**
- Historical Data
- Analytical Data

## Installation & requirements

```
python3 -m pip install truedata
```

- Python >= 3.10
- In-built dependencies: `websocket-client>=0.57.0`, `colorama>=0.4.3`, `python-dateutil>=2.8.1`, `pandas>=1.0.3`, `setuptools>=50.3.2`, `requests>=2.25.0`, `tqdm>=4.66.6`, `lz4==3.1.3` ("lz4 versions >3.1.3 currently have some errors and thus lz4 should not be upgraded till these dependency issues are resolved")

## Connecting / logging in

```python
# Real time only
from truedata import TD_live
td_obj = TD_live('<enter_your_login_id>', '<enter_your_password>')

# Historical only
from truedata import TD_hist
td_hist = TD_hist('<enter_your_login_id>', '<enter_your_password>')

# Both together
from truedata import TD_live, TD_hist
td_obj  = TD_live('<enter_your_login_id>', '<enter_your_password>')
td_hist = TD_hist('<enter_your_login_id>', '<enter_your_password>')
```

Constructor extras (seen in official docs/samples): `live_port=` (8082 default; 8084 alt; 8088 pushbeta; 9084 full feed), `url=` (`push.truedata.in` | `replay.truedata.in` | `pushbeta.truedata.in`), `log_level=`, `log_format=`, `log_handler=`, `full_feed=True`, `dry_run=False`, `compression` (v7 default on).

## Logging

```python
from truedata import TD_live
import logging
td_obj = TD_live('<id>', '<pwd>', live_port=realtime_port, url=url,
                 log_level=logging.WARNING, log_format="%(message)s")
```

- `log_level=logging.DEBUG` enables **Heartbeats** plus: (1) Market Status messages (market open/close), (2) automatic Touchline update messages, (3) Symbol add/remove messages, (4) Heartbeat messages (every 5 seconds). Use `WARNING` to silence.
- `LOG_LEVEL`, `LOG_FORMAT`, `LOG_HANDLER` all supported (stdlib logging).

## Real-time streaming

```python
symbols = ['<symbol_1>', '<symbol_2>', ...]
td_obj.start_live_data(symbols)

td_obj.live_data[symbol_1]           # latest tick (dataclass; replaced on each update)
td_obj.one_min_live_data[symbol_1]   # if subscribed to 1-min bars
td_obj.five_min_live_data[symbol_1]  # if subscribed to 5-min bars

ltp = td_obj.live_data[symbol_1].ltp
timestamp = td_obj.live_data[symbol_1].timestamp
```

`live_data` fields:

```
["timestamp","symbol_id","symbol","ltp","ltq","atp","ttq","day_open","day_high","day_low",
 "prev_day_close","oi","prev_day_oi","turnover","special_tag","tick_seq","best_bid_price",
 "best_bid_qty","best_ask_price","best_ask_qty","change","change_perc","oi_change","oi_change_perc"]
```

`one_min_live_data` / `five_min_live_data` fields:

```
["symbol","symbol_id","day_open","day_high","day_low","prev_day_close","prev_day_oi","oi","ttq",
 "timestamp","open","high","low","close","volume","change","change_perc","oi_change","oi_change_perc"]
```

### Callbacks (complete decorator set; while-loop methods are deprecated since v6)

"user need to define callback function for ticks or minute stream according to their subscription … please dont block the functions using any indefinite loops"

```python
@td_obj.trade_callback
def my_tick_data(tick_data): print("tick data", tick_data)

@td_obj.bidask_callback
def my_bidask_data(bidask_data): print("bid ask data", bidask_data)

@td_obj.one_min_bar_callback
def my_one_min_bar_data(bar_data): print("one min bar data", bar_data)

@td_obj.five_min_bar_callback
def my_five_min_bar_data(bar_data): print("five min bar data", bar_data)

@td_obj.greek_callback
def my_greek_data(greek_data): print("greek data", greek_data)

@td_obj.full_feed_trade_callback
def my_ff_trade_data(tick_data): print("full feed tick", tick_data)

@td_obj.full_feed_bar_callback
def my_ff_min_bar_data(bar_data): print("full feed bar", bar_data)
```

### Stop / disconnect

```python
td_obj.stop_live_data(['<symbol_1>', '<symbol_2>', ...])
td_obj.disconnect()
```

### Convert stream objects to dict

```python
td_obj.live_data[symbol_1].to_dict()
tick_data.to_dict()
```

## Greeks streaming

Fields: `["timestamp","symbol_id","symbol","iv","delta","theta","gamma","vega","rho"]`; dot access (`greek_data.delta`). Requires NSE options + greeks opt-in.

## Bid/Ask streaming

Fields: `["timestamp","symbol_id","symbol","bid","ask","total_bid","total_ask"]`.
- NSE: level 1 (one best bid/ask); `bidask_data.ask == [(ask, ask_qnty)]`
- BSE: level 2 (five best); `[(ask_1, ask_1_qnty, ask_1_no_of_trades), (ask_2, ...), ...]` — length 5 for bid and ask.

## Option chain streaming

See 05-analytics-optionchain-greeks.md §1 for the full argument table. Quick form:

```python
nifty_chain  = td_obj.start_option_chain('NIFTY', dt(2021, 8, 26))
sensex_chain = td_obj.start_option_chain("SENSEX", dt(2023, 9, 1), chain_length=80)
df = nifty_chain.get_option_chain()
nifty_chain.stop_option_chain()
```

Chain columns: `symbols, strike, type, ltp, ltt, ltq, volume, price_change, price_change_perc, oi, prev_oi, oi_change, oi_change_perc, bid, bid_qty, ask, ask_qty, iv, delta, theta, gamma, vega, rho`.

## Historical data (TD_hist)

All successful calls return **pandas DataFrames** (`df.to_dict(orient='records')` for list-of-dicts). Complete official method examples:

```python
from truedata import TD_hist
import logging
td_hist = TD_hist(username, password, log_level=logging.WARNING)

td_hist.get_historic_data('BANKNIFTY-I')                                        # 1-min bars, today
td_hist.get_historic_data('BANKNIFTY-I', duration='3 D')
td_hist.get_historic_data('BANKNIFTY-I', bar_size='30 mins')
td_hist.get_historic_data('BANKNIFTY-I', start_time=datetime.now()-relativedelta(days=3))
td_hist.get_historic_data('BANKNIFTY-I', end_time=datetime(2023, 11, 17, 12, 30))
td_hist.get_historic_data("BANKNIFTY-I", duration='2 D', bar_size='ticks', bidask=True, delivery=True)
td_hist.get_n_historical_bars('BANKNIFTY-I', no_of_bars=30, bar_size='5 min')
td_hist.get_n_historical_bars("BANKNIFTY-I", no_of_bars=10, bar_size='ticks')
td_hist.get_gainers("NSEEQ", topn=25)
td_hist.get_losers("NSEEQ", topn=25)
td_hist.get_bhavcopy('EQ', date=datetime(2023, 11, 16))
td_hist.get_bhavcopy('FO')
td_hist.get_bhavcopy('MCX')
result = td_hist.get_historic_data("NIFTY BANK", duration='1 D', bar_size='tick')
# result.to_dict(orient='records')
```

Defaults if omitted: `end_time = datetime.now()`, `duration = "1 D"`, `bar_size = "1 min"`.

**Limitations and caveats (verbatim):**
1. If you provide both duration and start time, duration will be used and start time will be ignored.
2. If you provide neither duration nor start time, duration = "1 D" will be used.
3. If you do not provide the bar size, bar_size = "1 min" will be used.
4. BAR_SIZES: `tick, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins, 30 mins, 60 mins, eod (EOD), week (WEEK), month (MONTH)`.
5. DURATION annotation: `D = Days, W = Weeks, M = Months, Y = Years`.

Bhavcopy: see 04-history-rest-api.md §7. Gainers/losers args: `segment` ∈ `NSEEQ, NSEFUT, NSEOPT, MCX`; `topn` default 10.

## Release notes (verbatim)

- **7.0.1** — bugfix for the compression
- **7.0.0** — all websocket messages are compressed for performance improvements by default
- **6.0.7** — bug fix and performance improved
- **6.0.5** — added compression in analytical calls for faster performance
- **6.0.4** — removing automatic reconnection after manual disconnection
- **6.0.3** — lz4 version released to latest one without prebuild
- **6.0.2** — analytics new methods added
- **6.0.0** — TD_live and TD_hist are two new modules under the package of truedata; option chain are coming with greeks; all historical calls return pandas dataframe; while-loop methods deprecated, only callback method; TD_analytics module added

Release history: 6.0.0 (Jan 3 2024) → 6.0.1/6.0.2 (Jan 15 2024) → 6.0.3 (Feb 3 2024) → 6.0.4 (May 10 2024) → 6.0.5 (Oct 7 2024) → 6.0.6 (Nov 7 2024) → 6.0.7 (Nov 9 2024) → 7.0.0 (Feb 2 2025) → 7.0.1 (Jul 14 2025); various 6.0.x/7.0.0 betas in between.

## Official sample programs (github.com/Nahas-N/Truedata-sample-code, folder `truedata/`)

| File | Shows |
|---|---|
| `callback_sample_code.py` | TD_live + TD_hist together; all 7 callbacks; `pushbeta.truedata.in:8088` |
| `full_feed_sample_code.py` | Full market feed: `TD_live(..., url="push.truedata.in", live_port=9084, full_feed=True, dry_run=False)` |
| `history_sample_code.py` | Every TD_hist method (as listed above) |
| `option_chain_sample_code.py` | 4 simultaneous chains (SBIN/NIFTY/BANKNIFTY/CRUDEOIL) with `bid_ask=True, greek=True` + live callbacks |
| `analytics_sample_code.py` | Every TD_analytics method (see 05-analytics-optionchain-greeks.md) |

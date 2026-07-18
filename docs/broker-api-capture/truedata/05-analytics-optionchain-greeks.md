# TrueData — Option Chain, Greeks & Analytics APIs

> Sources:
> - https://pypi.org/project/truedata/ (Option Chain Streaming, greeks fields, TD_analytics module)
> - https://pypi.org/project/truedata-ws/ (legacy option chain API)
> - https://raw.githubusercontent.com/Nahas-N/Truedata-sample-code/main/truedata/analytics_sample_code.py (official TD_analytics sample — complete method surface)
> - https://raw.githubusercontent.com/Nahas-N/Truedata-sample-code/main/truedata/option_chain_sample_code.py (official)
> - https://feedback.truedata.in/knowledge-base/article/how-to-fetch-options-chain-using-truedata-python-library
> - https://www.nuget.org/packages/TrueData-DotNet/ ("Analytics and Greeks - ADDON")

The analytics stack is an **add-on** on top of live/history subscriptions. Per the official .NET README:

```
Analytics and Greeks - ADDON
- Top Gainers / Losers
- Industry Gainer / Losers
- Options Greeks
```

Marketing page adds: "Live Option Chain API — get realtime data of entire option chain of the underlying in single call"; "Realtime Option Greeks API — Get IV, Delta, Theta, Vega, Gamma, & 16+ matrices in single call".

## 1. Option Chain Streaming (TD_live)

Stream one or more option chains into pandas DataFrames.

Chain columns (current `truedata` package, verbatim):

```
symbols, strike, type, ltp, ltt, ltq, volume, price_change, price_change_perc,
oi, prev_oi, oi_change, oi_change_perc, bid, bid_qty, ask, ask_qty,
iv, delta, theta, gamma, vega, rho
```

(legacy truedata-ws chain columns: `symbols, strike, type (CE/PE), ltp, ltt (last traded time), ltq (last traded qty), volume (total volume), price change, price change %, oi, oi Change, OI Change %, bid, bid qty, ask, ask qty` — greeks columns were added when chains started "coming with greeks" in truedata v6.0.0.)

### Start a chain

```python
from datetime import datetime as dt
nifty_chain  = td_obj.start_option_chain('NIFTY', dt(2021, 8, 26))
sensex_chain = td_obj.start_option_chain('SENSEX', dt(2023, 9, 1), chain_length=80)   # current pkg
# legacy (truedata-ws) required bse_option=True for SENSEX/BANKEX chains:
sensex_chain = td_obj.start_option_chain("SENSEX", dt(2023, 9, 1), chain_length=80, bse_option=True)
```

`start_option_chain` arguments (current package, verbatim list):

| Arg | Meaning | Default |
|---|---|---|
| `symbol` | e.g. `NIFTY`, `BANKNIFTY`, `SBIN` (underlying; MCX e.g. `CRUDEOIL` also works per official sample) | — |
| `expiry` | expiry date (`datetime` object) | — |
| `chain_length` | number of strikes pulled around the future price | `10` |
| `bid_ask` | enable live quotes in chain | `False` |
| `greek` | stream greeks along with option chain | `False` |

Legacy-only args: `market_open_post_hours` (default `False`; set `True` for special sessions e.g. Muhurat trading — renamed from `market_close` in 4.0.7), `bse_option` (True for SENSEX/BANKEX chains).

### Pull / stop

```python
df = nifty_chain.get_option_chain()   # returns the chain DataFrame; callable anywhere, any frequency
nifty_chain.stop_option_chain()       # stop updating this chain
```

Notes (official):
- Chains work for tick and 1-min bar streaming subscriptions, and also pre/post market via touchline data.
- Option chains include Previous OI, OI Change & Volume since truedata-ws 4.0.7; LTQ, price change, price change % since 4.2.3; currency option chains since 4.0.8.
- Other subscribed symbols keep streaming while chains run.
- Official multi-chain example subscribes NIFTY, BANKNIFTY, SBIN and CRUDEOIL chains simultaneously with `bid_ask=True, greek=True` and polls `get_option_chain()` every 5s (option_chain_sample_code.py).
- KB video: https://www.youtube.com/embed/GEYRrOzvDfU ("How to fetch Options Chain using TrueData Python Library").

## 2. Greeks streaming (TD_live)

```python
@td_obj.greek_callback
def mygreek_callback(greek_data):
    print("Greek > ", greek_data)
```

Fields (verbatim): `["timestamp","symbol_id","symbol","iv","delta","theta","gamma","vega","rho"]` — dot access (`greek_data.delta` etc.). Requires NSE options subscription + greeks enablement. Greeks field order fixed in truedata-ws 5.0.7.

## 3. TD_analytics module (analytics REST add-on)

Introduced in `truedata` v6.0.0 ("TD_analytics module is added to package"); "analytics new methods added" in 6.0.2; compression added to analytical calls in 6.0.5.

```python
import logging
from truedata import TD_analytics
from datetime import date as d

my_analytics = TD_analytics(username, password, log_level=logging.DEBUG)
```

Complete method surface from the official sample (verbatim calls):

```python
# 1. OI gainers/losers
gainer = my_analytics.get_oi_gainer_losers(top=200, series="XX", sort_by="OiGainersPriceGainers")

# 2. Index constituents gainers/losers
gainer = my_analytics.get_index_gainer_losers(top=20, segment="EQ", index_name="NIFTY 500", sort_by="gainers")

# 3. Industry gainers/losers
gainer = my_analytics.get_industry_gainer_losers(top=20, segment="EQ", industry="Information Technology", sort_by="gainers")

# 4. Option chain snapshot (with greeks)
chain = my_analytics.get_option_chain(symbol="NIFTY", expiry=d(2023, 12, 28), greeks=True)

# 5. Historical greeks for a specific option contract
hist_greeks = my_analytics.get_history_greeks(symbol="RELIANCE", expiry=d(2023, 12, 28), strike=2400, series="CE")

# 6. Latest (LTP) greeks for a specific option contract
ltp_greeks = my_analytics.get_history_greeks(symbol="RELIANCE", expiry=d(2023, 12, 28), strike=2400, series="CE", ltp=True)
```

| Method | Parameters (observed) | Purpose |
|---|---|---|
| `get_oi_gainer_losers` | `top` (int), `series` (e.g. `"XX"` = futures series), `sort_by` (e.g. `"OiGainersPriceGainers"`) | OI gainers/losers screener |
| `get_index_gainer_losers` | `top`, `segment` (`"EQ"`), `index_name` (`"NIFTY 500"` etc.), `sort_by` (`"gainers"`/`"losers"`) | Gainers/losers within an index |
| `get_industry_gainer_losers` | `top`, `segment`, `industry` (e.g. `"Information Technology"`), `sort_by` | Gainers/losers within an industry |
| `get_option_chain` | `symbol`, `expiry` (date), `greeks` (bool) | One-shot full option chain (REST, not streaming) |
| `get_history_greeks` | `symbol`, `expiry`, `strike`, `series` (`"CE"`/`"PE"`), `ltp` (bool → latest only) | Historical / latest greeks for one contract |

All analytics calls return pandas DataFrames (consistent with the package's history behaviour).

Related REST analytics on the history host (see 04-history-rest-api.md): `getTopGainers`, `getTopLosers`, `getTopVolumeGainers` (`topsegment` ∈ NSEFUT, NSEEQ, NSEOPT, CDS, MCX), `getCorpAction(symbol)`, `getSymbolNameChange()`, `Get52WeekHighLow(symbol)` (.NET), `getLTP(symbol)`.

## 4. Corporate Data API (TrueWealth)

Separate product ("Corporate Data API" / TrueWealth — https://wealth.truedata.in): real-time corporate announcements, AI-powered disclosure analysis, financial results & ratio insights, shareholding patterns, corporate actions, company news feeds; fundamental & technical screeners "coming soon". Access/pricing via sales (no public endpoint docs; select "Fundamental Data & Corporate Information (API)" options — Real Time Announcements, Financial Results, Share Holding Patterns, Financial Ratios, Mutual Funds, AI summaries — on the API signup form).

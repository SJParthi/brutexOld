# 08 — Official SDKs (Python, Node.js, .NET) & sample code

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). All registry pages + package sources in this
file were fetched DIRECTLY (high confidence).

## 1. PyPI `truedata` — the CURRENT Python SDK

| Claim | Value |
|---|---|
| Latest | **7.0.1**, released 2025-07-14 (`pip install truedata`; Python ≥ 3.10) |
| Dependencies | websocket-client≥0.57.0, colorama≥0.4.3, python-dateutil≥2.8.1, pandas≥1.0.3, setuptools≥50.3.2, requests≥2.25.0, tqdm≥4.66.6, **lz4==3.1.3** |
| Modules | `TD_live` (websocket), `TD_hist` (REST history), `TD_analytics` (analytics/greeks) — split since v6.0.0 ("Module separation (TD_live, TD_hist), pandas dataframe returns") |
| Author / license | Nahas N (nahas@truedata.in — vendor staff); GPLv3 |
| Homepage link | github.com/kapilmar/truedata-ws — **DEAD (404)** as of 2026-07-15 |
| Sample code | github.com/Nahas-N/Truedata-sample-code — `analytics_sample_code.py`, `callback_sample_code.py`, `full_feed_sample_code.py`, `history_sample_code.py`, `option_chain_sample_code.py` |
| Changelog highlights | v7.0.1 compression bugfix · v7.0.0 WS message compression · v6.0.5 compression for analytical calls · v6.0.0 module split + DataFrame returns (20 releases 6.0.0 → 7.0.1; 7.0.0b1 2024-12-27, 7.0.0b2 2025-01-29) |

### Constructors (pkg-source, verbatim defaults)

```python
TD_live(login_id, password, url='push.truedata.in', live_port=8084,
        log_level=logging.WARNING, log_handler=None, log_format=None,
        full_feed=False, dry_run=False, change_bar=False, compression=False)
TD_hist(login_id, password, log_level=..., log_handler=..., log_format=...)   # REST-only; "NO PORT, broker token needed from now on"
TD_analytics(login_id, password, log_level=..., ...)
```

- WS URL built: `wss://{url}:{live_port}?user={login_id}&password={password}`.
- The vendor full-feed sample uses `live_port=9084` with `full_feed=True`
  (github-fetch) — the distinct full-market-feed port. Port map so far:
  8082 (live, older default) · 8084 (live, v7 default; "if authorized") ·
  9084 (full feed, sample) · 9092 (corporate WS).

### TD_live surface (PyPI description, high)

`start_live_data(symbols)` · `stop_live_data(symbols)` · `disconnect()` ·
`start_option_chain(symbol, expiry_date, chain_length=10, bid_ask=False, greek=False)` ·
`get_bhavcopy(segment)` · `get_gainers/get_losers(segment, topn)`;
state dicts `live_data`, `one_min_live_data`, `five_min_live_data`,
`touchline_data`, `greek_data`; callbacks `@trade_callback`,
`@bidask_callback`, `@one_min_bar_callback`, `@five_min_bar_callback`,
`@greek_callback`, `@full_feed_trade_callback`, `@full_feed_bar_callback`.

### TD_hist surface (PyPI + vendor samples, high)

```python
get_historic_data(symbol, duration='1 D', bar_size='1 min', start_time=..., end_time=..., bidask=False, delivery=False)
get_n_historical_bars(symbol, no_of_bars=30, bar_size='5 min')
get_gainers/get_losers(segment='NSEEQ', topn=25)
get_bhavcopy('EQ'|'FO'|'MCX', date=datetime(...))
```

bar_size: `tick(s), 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins,
30 mins, 60 mins, eod, week, month`; duration `N D|W|M|Y`. Vendor samples
confirm tick + bidask + delivery combos
(`get_historic_data("BANKNIFTY-I", duration='2 D', bar_size='ticks', bidask=True, delivery=True)`)
and spot-index queries (`get_historic_data("NIFTY BANK", duration='1 D', bar_size='tick')`).

### TD_analytics surface

See [05-analytics-corporate.md](./05-analytics-corporate.md) §2. Sample
params: `get_oi_gainer_losers(top=200, series="XX", sort_by="OiGainersPriceGainers")`;
`get_index_gainer_losers(top=20, segment="EQ", index_name="NIFTY 500", sort_by="gainers")`;
`get_industry_gainer_losers(top=20, segment="EQ", industry="Information Technology", sort_by="gainers")`;
`get_option_chain(symbol="NIFTY", expiry=date(2023,12,28), greeks=True)`;
`get_history_greeks(symbol="RELIANCE", expiry=date(2023,12,28), strike=2400, series="CE"[, ltp=True])`.

### Verbatim vendor caveat (PyPI description)

> "Kindly note that data that is not enabled by default may require
> exchange approvals, which vary depending on the specific exchange and
> segment in question."

## 2. PyPI `truedata-ws` — the OLDER SDK (still published)

- Latest **5.0.11**, 2023-11-16; Python ≥ 3.7; class `TD`
  (`from truedata_ws.websocket.TD import TD`); defaults
  `live_port=8082, url='push.truedata.in'`; historical-only via
  `live_port=None`; realtime-only via `historical_api=False`.
- 62 releases from 0.1.15. Milestones: v3.0.1 moved historical from WS to
  REST ("Historical WebSocket deprecated; REST API now used"); v4.3.1
  renamed `min_live_data` → `one_min_live_data` (+5-min); v5.0.1 BidAsk
  L2 for BSE F&O + dataclasses; v5.0.10 L2 totals + TD Analytics; v5.0.11
  option-chain reconnect fix.
- BSE option chains: `start_option_chain('SENSEX', dt(2023,9,1),
  chain_length=80, bse_option=True)`.
- Author Kapil Marwaha (kapil@truedata.in). Same dead homepage link.

## 3. Node.js `truedata-nodejs`

- Latest **1.2.0** (2026-05-08); 1.1.0 (2026-01-14); maintainer
  `truedata1 <support@truedata.in>`; MIT.
- Surface: `rtConnect / rtConnectFullFeed / rtSubscribe / rtUnsubscribe /
  rtDisconnect / isSocketConnected / getMarketStatus`; `historical.*`
  (11 REST calls); `rtCorpConnect` + `corporate.*` (27 corporate calls);
  `startOptionChain / stopOptionChain`; events `touchline | tick | greeks
  | bidask | bidaskL2 | bar | marketstatus | heartbeat | optionChain |
  message`.

## 4. .NET `TrueData-DotNet`

- **1.4.7** (2025-07-29); .NET Standard 2.0+; deps K4os.Compression.LZ4 +
  Newtonsoft.Json; MIT; ~23.8K downloads; several older versions
  deprecated "due to critical bugs" (NuGet page).
- Classes: `TDWebSocket(id, pwd, url, port)` (events OnConnect /
  OnDataArrive / OnClose / OnError), `TDHistory` (GetBarHistory,
  GetTickHistory, GetLastNBars, GetLastNTicks, GetBhavCopyStatus,
  GetBhavCopy, GetTopGainers, GetTopLosers, GetCorporateActions,
  Get52WeekHighLow), `TDMaster` (master downloads).

## 5. Adapter guidance (BruteX/TickVault)

The vendor SDKs are maintained and reasonably featureful (unlike the
GDF alpha packages), but any BruteX/TickVault adapter should still target
the WIRE protocol directly ([02](./02-authentication.md)–[04](./04-historical-rest.md)):
the house rules (structlog, typed errors, injected `rate_common`
controllers, int-paisa boundaries — CLAUDE.md §4) don't compose with
pandas-returning GPLv3 clients. The SDK sources remain the authoritative
protocol REFERENCE, and their client-side disciplines (token early-renew
buffer, 1 s stagger on quota breach, heartbeat-gap reconnect, daily
master refresh) are worth mirroring.

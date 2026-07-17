# TrueData — Historical Data REST API (history.truedata.in)

> Sources:
> - https://wstest.truedata.in/ (official sandbox — auth + history request panels)
> - https://www.npmjs.com/package/truedata-nodejs (official README: complete function list = REST endpoint surface, parameters, defaults, enums)
> - https://www.nuget.org/packages/TrueData-DotNet/ (official README: TDHistory methods incl. 52-week high/low, corporate actions)
> - https://pypi.org/project/truedata/ and https://pypi.org/project/truedata-ws/ (official Python history docs)
> - https://feedback.truedata.in/knowledge-base/article/historical-data-availability-through-rest-api
> - https://feedback.truedata.in/knowledge-base/article/historical-real-time-data-availability-through-market-data-api
> - https://feedback.truedata.in/knowledge-base/article/requestscalls-limit-via-historical-and-real-time-api
> - https://feedback.truedata.in/knowledge-base/article/how-to-get-eod-data-in-truedata-market-data-api-sandbox-page
> - https://feedback.truedata.in/knowledge-base/article/how-can-we-get-multiple-symbol-data-in-one-go-for-historical-api-call

History was originally served over WebSocket; that was **deprecated and terminated on 30 Apr 2021** and replaced with REST/HTTP (official Telegram announcement: "HISTORICAL DATA VIA WEBSOCKET - SERVICE WILL TERMINATE on 30 APR 2021 … shift immediately to the new and Blazing Superfast REST API service"; truedata-ws 3.0.1: "Historical feed via Websocket - Deprecated; Historical feed via REST - Added". Rationale: "These REST APIs would help us add more features and allow for much faster downloads of historical data.").

Official raw-API artifacts announced with Documentation v2.1 (Telegram msg 92; Dropbox links now dead/gated — request current copies from apisupport@truedata.in):

- `TrueData Market Data API Documentation v 2.1.pdf` — https://www.dropbox.com/s/ftxetq7mbqjh9e7/TrueData%20Market%20Data%20API%20Documentation%20v%202.1.pdf?dl=0 (v2.2 later on Scribd: https://www.scribd.com/document/667918775/TrueData-Market-Data-API-Documentation-v-2-2-1)
- **Postman collection**: `TrueData_History_REST_API_v1.1.postman_collection.zip` — https://www.dropbox.com/s/mon2horjnx9nt97/TrueData_History_REST_API_v1.1.postman_collection.zip?dl=0
- Entire documentation folder — https://www.dropbox.com/sh/cpba3vkccsfapkh/AADIBn36OaBbiu_jMXzXRQdka?dl=0

v2.1 documentation highlights (verbatim from announcement): "1) New Historical REST implemented 2) Historical Websocket - Deprecated (Closure date - 30 April 2021) 3) New feature > get bhavcopy eod status, get bhavcopy 4) New feature > get LTP (via REST) 5) Postman collection provided for faster integration."

## 1. Base URLs & auth

```
Auth:    POST https://auth.truedata.in/token        (username, password, grant_type=password)
Data:    GET  https://history.truedata.in/<method>  (Authorization: Bearer <token>)
```

See 02-authentication-connection.md for the token flow. Responses: `json` (default) or `csv` via the `response` parameter. Optionally include TrueData symbol ids in responses via `getSymbolId=1`.

## 2. Endpoint surface (as exposed 1:1 by the official Node.js SDK)

The official `truedata-nodejs` `historical.*` functions map directly onto the REST methods; their names, parameters and defaults below are verbatim from the official README:

| Function (≡ REST method) | Signature & defaults | Notes |
|---|---|---|
| getTickData | `historical.getTickData(symbol, from, to, bidask = 1, response = 'json', getSymbolId = 0)` | Max up to last 5 days of tick data |
| getBarData | `historical.getBarData(symbol, from, to, interval = '1min', response = 'json', getSymbolId = 0)` | OHLCV(+OI) bars |
| getLastNTicks | `historical.getLastNTicks(symbol, nticks = 2000, bidask = 1, response = 'json', getSymbolId = 0)` | Max up to last 5 days of tick data |
| getLastNBars | `historical.getLastNBars(symbol, nbars = 200, interval = '1min', response = 'json', getSymbolId = 0)` | `interval = '1min' OR 'EOD'` |
| getBhavCopyStatus | `historical.getBhavCopyStatus(segment = 'FO', response = 'json')` | Checks latest completed bhavcopy availability |
| getBhavCopy | `historical.getBhavCopy(segment = 'FO', date, response = 'json')` | Current date default; date format `'yyyy-mm-dd'` |
| getLTP | `historical.getLTP(symbol, bidask = 1, response = 'json', getSymbolId = 0)` | Snap LTP |
| getTopGainers | `historical.getTopGainers(topsegment = 'NSEFUT', top = 50, response = 'json')` | |
| getTopLosers | `historical.getTopLosers(topsegment = 'NSEFUT', top = 50, response = 'json')` | |
| getTopVolumeGainers | `historical.getTopVolumeGainers(topsegment = 'NSEFUT', top = 50, response = 'json')` | |
| getCorpAction | `historical.getCorpAction(symbol, response = 'json')` | Corporate actions for a symbol |
| getSymbolNameChange | `historical.getSymbolNameChange(response = 'json')` | Symbol renames master |

.NET `TDHistory` adds/mirrors (official NuGet README):

```csharp
tDHistory.GetBarHistory("ACC", DateTime.Today.AddHours(-1), DateTime.Now, false, Constants.Interval_1Min, false); // GetBars - OHLC
tDHistory.GetTickHistory("ACC", false, DateTime.Now.AddHours(-0.5), DateTime.Now, false, true);                   // GetTicks
tDHistory.GetLastNBars("HDFC", 1, false, "15min");
tDHistory.GetLastNTicks("HDFC", true, 1);
tDHistory.GetBhavCopyStatus("BSEFO", true);
tDHistory.GetBhavCopy("EQ", DateTime.Today, true);
tDHistory.GetTopGainers("NSEEQ", true, 10);
tDHistory.GetTopLosers("NSEEQ", true, 10);
tDHistory.GetCorporateActions("AARTIIND", true);   // Corporate Actions
tDHistory.Get52WeekHighLow("RELIANCE");            // 52 Week High Low for Symbol
```

(The boolean parameters select CSV/JSON response and symbol-id inclusion, matching the REST `response` and `getSymbolId` params. REST APIs also cover: Bar/OHLC Data, Ticks Data, Gainers/Losers, Bhavcopy — per the .NET README's "REST APIs" list.)

## 3. Parameters, enums & defaults (verbatim from official README)

| Parameter | Default | Available values |
|---|---|---|
| `response` | `'json'` | `'json'`, `'csv'` |
| `bidask` | `1` | `1` = bidask feed active, `0` = inactive |
| `interval` | `'1min'` | `'1min'`, `'2min'`, `'3min'`, `'5min'`, `'10min'`, `'15min'`, `'30min'`, `'60min'`, `'EOD'` |
| `nticks` | `2000` | any number (max = last 5 days of ticks) |
| `nbars` | `200` | any number |
| `getSymbolId` | `0` | `1` = symbolId active, `0` = inactive |
| `segment` (bhavcopy) | `'FO'` | `'FO'`, `'EQ'`, `'MCX'`, `'BSEFO'`, `'BSEEQ'` |
| `topsegment` (gainers/losers) | `'NSEFUT'` | `'NSEFUT'`, `'NSEEQ'`, `'NSEOPT'`, `'CDS'`, `'MCX'` |
| `top` | `50` | any number |

### Time range: from/to or duration

- `from` / `to` timestamps use format `YYMMDDTHH:mm:ss` (e.g. `'210302T09:00:00'`), 24-hour clock. Node helper: `formatTime(year, month, date, hour, minute)`.
- OR pass `duration` instead of from/to:

```
'5D'  // D for Day
'3W'  // W for Week
'2M'  // M for Month
'1Y'  // Y for Year
```

e.g. `historical.getTickData(symbol, duration = '1D', bidask = 1, response = 'json')`,
`historical.getBarData(symbol, duration = '2W', interval = '1min', response = 'json')`.

- `symbol` is the symbol NAME (e.g. `'NIFTY-I'`, `'RELIANCE'`, `'NIFTY 50'`, `'L&TFH'`).

### Node examples (verbatim)

```js
historical.auth(user, pwd);

historical
  .getBarData('NIFTY-I', '210302T09:00:00', '210302T15:30:00', interval = '1min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res))
  .catch((err) => console.log(err));

historical
  .getBarData('NIFTY 50', duration = '1W', interval = '60min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res));

historical
  .getTickData('SBIN', '1D', bidask = 1, response = 'csv', getSymbolId = 0)
  .then((res) => console.log(res));

historical.getLTP('L&TFH').then((res) => console.log(res));
```

All Node history calls return Promises (`.then/.catch` or `async/await`).

## 4. Response shape

Sandbox history table columns (https://wstest.truedata.in/): `Sr | Timestamp | LTP | O | H | L | C | V | OI` — i.e.:

- **Bar records**: `timestamp, open (o), high (h), low (l), close (c), volume (v), oi`
- **Tick records**: `timestamp, ltp, volume/ltq (+ bid, bidqty, ask, askqty when bidask=1; + delivery volume when requested)`

(Python: strategy examples index tick history by `x['ltp']` and bar history by `x['c']`, and `current_BN_Spot.iloc[0].timestamp / .ltp` — confirming lowercase keys `ltp`, `c`, `timestamp` in JSON records. EOD bars include a delivery-data parameter since truedata-ws 4.3.3: "Delivery data parameter added to EOD Data"; the current Python lib exposes `delivery=True` on tick history: `get_historic_data("BANKNIFTY-I", duration='2 D', bar_size='ticks', bidask=True, delivery=True)`. The sandbox exposes the same as a "DelVolume" checkbox.)

## 5. Data availability windows (official)

- **Tick data**: last **5 trading days**
- **Bar data (1/2/3/5/10/15/30/60 min)**: last **6 months**
- **Daily (EOD) bars**: **10+ years**
- "We are soon going to be enabling Extended History, which would be available as an Add-on in case you would need more history."

## 6. Rate limits (official)

| Data | Per second | Per minute | Per hour |
|---|---|---|---|
| Tick data requests | 5 | 300 | 18000 |
| Minutes-bar data requests | 10 | 600 | 18000 |

- Real-time API has **no** such request limit (streaming).
- One symbol per historical request: "Historical data needs to be requested and can be delivered 1 by 1. Currently, there is no method to send you the history of multiple symbols at the same time." Workaround for bulk latest-bar needs: subscribe to 1-min/5-min bar streaming (see 03-realtime-websocket.md §6).
- Rate limiting is "handled gracefully" by the Python library since truedata-ws 4.2.3.

## 7. Bhavcopy behaviour (official Python README)

```python
eq_bhav  = td_hist.get_bhavcopy('EQ')
fo_bhav  = td_hist.get_bhavcopy('FO')
mcx_bhav = td_hist.get_bhavcopy('MCX')
specific = td_hist.get_bhavcopy('EQ', date=datetime(2023, 11, 16))
```

"The request checks if the latest completed bhavcopy has arrived for that segment and, if arrived, it returns the data. In case it has not arrived it provides the date and time of the last bhavcopy available which can also be pulled by providing the bhavcopy date."

Error text example (verbatim): `No complete bhavcopy found for requested date. Last available for 2021-03-19 16:46:00.`

## 8. Gainers / losers (Python surface)

```python
gainers = td_hist.get_gainers(segment="NSEEQ", topn=10)   # segment: NSEEQ, NSEFUT, NSEOPT, MCX
losers  = td_hist.get_losers(segment="NSEEQ", topn=10)    # topn default 10
```

(legacy truedata-ws also had `df_style` bool for DataFrame vs list output; current `truedata` returns pandas DataFrames for all successful history calls — convert with `df.to_dict(orient='records')`.)

## 9. EOD via sandbox (KB steps, verbatim)

1. Visit https://wstest.truedata.in
2. Step 1: Login using your TrueData API login credentials
3. Step 2: Enter the symbol code
4. Step 3: From the Interval dropdown select **EOD**
5. Step 4: Select the duration (From/To)
6. Step 5: The fetched data is displayed

Interval dropdown values on the sandbox: `1min, 2min, 3min, 5min, 10min, 15min, 30min, EOD, Tick` (+ `DelVolume` checkbox).

## 10. Python-side history parameters (see 07-python-sdk.md for full API)

- Defaults when omitted: `end_time = datetime.now()`, `duration = "1 D"`, `bar_size = "1 min"`.
- BAR_SIZES: `tick, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins, 30 mins, 60 mins, eod/EOD, week/WEEK, month/MONTH` (week & month added in truedata-ws 4.3.2 — aggregated from EOD server-side/lib-side).
- DURATION units: `D`(ays), `W`(eeks), `M`(onths), `Y`(ears).
- If both `duration` and `start_time` given, duration wins.
- `get_n_historical_bars(symbol, no_of_bars=N, bar_size=...)` is enabled for **Tick, 1 min & EOD** bars.

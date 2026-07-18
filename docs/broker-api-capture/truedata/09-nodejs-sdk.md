# TrueData — Official Node.js SDK: `truedata-nodejs`

> Source: https://www.npmjs.com/package/truedata-nodejs (fetched 2026-07-15)
> Covers: WebSocket APIs (live data) + REST APIs (historical data)

## Install & import

```
npm install truedata-nodejs
```

```js
const { rtConnect, rtDisconnect, rtSubscribe, rtUnsubscribe, rtFeed,
        historical, formatTime, isSocketConnected } = require('truedata-nodejs')
```

## Real-time (WebSocket)

```js
const user = 'your username'
const pwd  = 'your password'
const port = 8082
const symbols = ['NIFTY-I', 'BANKNIFTY-I', 'CRUDEOIL-I'];   // symbols in array format

rtConnect(user, pwd, symbols, port, bidask = 1, heartbeat = 1, replay = 0, url = 'push');
```

### rtConnect parameter defaults (verbatim table)

| Default values | Available values |
|---|---|
| `bidask = 1` | `0, 1` — 1 = bidask active, 0 = bidask inactive |
| `heartbeat = 1` | `0, 1` — 1 = print heartbeat message, 0 = don't print |
| `replay = 0` | `0, 1` — 1 = Feed replay, 0 = Normal feed (default if parameter not entered) |
| `url = 'push'` | `'push'` (host prefix; replay via `replay = 1`) |

### Event handlers (complete set)

```js
rtFeed.on('touchline', touchlineHandler);      // Receives Touchline Data
rtFeed.on('tick', tickHandler);                // Receives Tick data
rtFeed.on('greeks', greeksHandler);            // Receives Greeks data
rtFeed.on('bidask', bidaskHandler);            // Receives Bid Ask data if enabled
rtFeed.on('bidaskL2', bidaskL2Handler);        // Receives level 2 Bid Ask data only for BSE exchange
rtFeed.on('bar', barHandler);                  // Receives 1min and 5min bar data
rtFeed.on('marketstatus', marketStatusHandler);// Receives marketstatus messages
rtFeed.on('heartbeat', heartbeatHandler);      // Receives heartbeat message and time

function touchlineHandler(touchline){ console.log(touchline) }
function tickHandler(tick){ console.log(tick) }
function greeksHandler(greeks){ console.log(greeks) }
function bidaskHandler(bidask){ console.log(bidask) }
function bidaskL2Handler(bidaskL2){ console.log(bidaskL2) }
function barHandler(bar){ console.log(bar) }
function marketStatusHandler(status){ console.log(status) }
function heartbeatHandler(heartbeat){ console.log(heartbeat) }
```

### Utility / control functions

```js
isSocketConnected()        // Check if socket is connected or not. Returns Boolean
getMarketStatus()          // Return the current market status
rtSubscribe(newSymbols)    // Dynamically subscribe to new symbols (array)
rtUnsubscribe(oldSymbols)  // Dynamically unsubscribe from currently subscribed symbols (array)
rtDisconnect()             // Disconnect live data feed and close socket connection
```

Reconnection: "The library will check for internet connection and once steady will try to re-connect the Websocket."

## Historical (REST)

```js
historical.auth(user, pwd);                      // For authentication

from = formatTime(2021, 3, 2, 9, 15)             // (year, month, date, hour, minute) 24-hour
to   = formatTime(2021, 3, 5, 9, 15)
```

All API calls return a Promise (`.then/.catch` or `async/await`).

### Complete function list (verbatim)

```js
historical.getTickData (symbol, from, to, bidask = 1, response = 'json', getSymbolId = 0)  // Max upto last 5 days of tick data
historical.getBarData (symbol, from, to, interval = '1min', response = 'json', getSymbolId = 0)
historical.getLastNTicks (symbol, nticks = 2000, bidask = 1, response = 'json', getSymbolId = 0)  // Max upto last 5 days
historical.getLastNBars (symbol, nbars = 200, interval = '1min', response = 'json', getSymbolId = 0)  // interval = '1min' OR 'EOD'
historical.getBhavCopyStatus (segment = 'FO', response = 'json')
historical.getBhavCopy (segment = 'FO', date, response = 'json')
historical.getLTP (symbol, bidask = 1, response = 'json', getSymbolId = 0)
historical.getTopGainers (topsegment = 'NSEFUT', top = 50, response = 'json')
historical.getTopLosers (topsegment = 'NSEFUT', top = 50, response = 'json')
historical.getTopVolumeGainers (topsegment = 'NSEFUT', top = 50, response = 'json')
historical.getCorpAction (symbol, response = 'json')
historical.getSymbolNameChange (response = 'json')
```

Duration form instead of from/to:

```js
historical.getTickData (symbol, duration = '1D', bidask = 1, response = 'json')
historical.getBarData (symbol, duration = '2W', interval = '1min', response = 'json')
// '5D' D for Day | '3W' W for Week | '2M' M for Month | '1Y' Y for Year
```

### Parameter defaults (verbatim table)

| Default values | Available values |
|---|---|
| `response = 'json'` | `'json'`, `'csv'` |
| `bidask = 1` | `1, 0` — 1 = bidask feed active, 0 = inactive |
| `interval = '1min'` | `'1min','2min','3min','5min','10min','15min','30min','60min','EOD'` |
| `nticks = 2000` | any number (max last 5 days of ticks) |
| `nbars = 200` | any number |
| `getSymbolId = 0` | `1, 0` — 1 = symbolId active |
| `segment = 'FO'` | `'FO','EQ','MCX','BSEFO','BSEEQ'` |
| `topsegment = 'NSEFUT'` | `'NSEFUT','NSEEQ','NSEOPT','CDS','MCX'` |
| `top = 50` | any number |

Notes (verbatim): *symbol in historical api is symbol name, e.g. `'NIFTY-I'` or `'RELIANCE'`. *In `getBhavCopy`, current date is default; date format `'yyyy-mm-dd'`.

### Examples (verbatim)

```js
historical
  .getBarData('NIFTY-I', '210302T09:00:00', '210302T15:30:00', interval = '1min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res))
  .catch((err) => console.log(err));

historical
  .getBarData('NIFTY-I', duration = '3W', interval = '1min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res))
  .catch((err) => console.log(err));

historical
  .getBarData('RELIANCE', from, to, interval = '1min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res));

historical
  .getBarData('NIFTY 50', duration = '1W', interval = '60min', response = 'json', getSymbolId = 0)
  .then((res) => console.log(res));

historical
  .getTickData('SBIN', '1D', bidask = 1, response = 'csv', getSymbolId = 0)
  .then((res) => console.log(res));

historical
  .getLTP('L&TFH')
  .then((res) => console.log(res));
```

### Full combined example (verbatim skeleton)

```js
const { rtConnect, rtSubscribe, rtUnsubscribe, rtFeed, historical, formatTime } = require('truedata-nodejs');
const user = 'your username';
const pwd = 'your password';
const port = 8082;
const symbols = ['NIFTY-I', 'BANKNIFTY-I', 'CRUDEOIL-I'];

rtConnect(user, pwd, symbols, port, (bidask = 1), (heartbeat = 1));
rtFeed.on('touchline', touchlineHandler);
rtFeed.on('tick', tickHandler);
rtFeed.on('greeks', greeksHandler);
rtFeed.on('bidask', bidaskHandler);
rtFeed.on('bidaskL2', bidaskL2Handler);
rtFeed.on('bar', barHandler);
rtFeed.on('marketstatus', marketStatusHandler);
rtFeed.on('heartbeat', heartbeatHandler);

historical.auth(user, pwd);
from = formatTime(2021, 3, 2, 9, 15);
to   = formatTime(2021, 3, 5, 9, 15);
```

Replay note (from KB): `rtConnect(user, pwd, symbols, port, bidask = 1, heartbeat = 1, replay = 1);` streams the post-market replay feed.

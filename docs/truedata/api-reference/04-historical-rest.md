# 04 — Historical REST API (endpoints, params, depth, rate limits)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Base: `https://history.truedata.in` — bearer
auth ([02](./02-authentication.md)). All endpoints are GET with query
params. The server responds CSV by default; the SDKs request
`response=csv` and convert client-side; the KB says the service supports
"CSV or JSON formats" (search-relay, medium).

## 1. Endpoint map (vendor SDK source — pkg-source, HIGH)

From `truedata-nodejs@1.2.0 utils.js routes` + `truedata@7.0.1
Historical_REST.py`:

| Endpoint | Path | Query params (as built by the SDKs) |
|---|---|---|
| Bars | `/getbars` | `symbol, from, to, response=csv, interval` (+ Python adds `comp=true`, `bidask=0/1`, `delivery=true` when `bar_size=='eod'`) |
| Ticks | `/getticks` | `symbol, bidask=0/1, from, to, response=csv` (+ Python `comp=true`) |
| Last-N bars | `/getlastnbars` | `symbol, response=csv, nbars, interval(1min|EOD), bidask=0` |
| Last-N ticks | `/getlastnticks` | `symbol, response=csv, nticks, interval=tick, bidask=0/1` (LTP = this with `nticks=1`) |
| Bhavcopy | `/getbhavcopy` | `segment, date=YYYY-MM-DD, response=csv` |
| Bhavcopy status | `/getbhavcopystatus` | `segment, response=csv` |
| Top gainers | `/gettopngainers` | `segment, response=csv, topn` |
| Top losers | `/gettopnlosers` | `segment, response=csv, topn` |
| Top volume gainers | `/gettopnvolumegainers` | `segment, response=csv, topn` |
| Corporate actions | `/getcorpaction` | `symbol, response=csv` |
| Symbol name changes | `/getsymbolchangehistory` | `response=csv&SYMBOL=` |

Worked example (npm README, direct-fetch):
`getBarData('NIFTY-I', '210302T09:00:00', '210302T15:30:00', interval='1min', response='json', getSymbolId=0)`.

## 2. Time & parameter conventions (pkg-source, high)

- **Timestamp format**: `YYMMDDTHH:MM:SS` (2-digit year), e.g.
  `210302T09:00:00`. Node defaults: from=`<today>T09:00:00`,
  to=`<today>T23:59:00`.
- **Duration alternative**: `'1D' / '3W' / '2M' / '1Y'` (D/W/M/Y)
  instead of from/to.
- **Intervals** (npm README, direct-fetch): `1min, 2min, 3min, 5min,
  10min, 15min, 30min, 60min, EOD`. Python bar_size list (PyPI,
  direct-fetch): `tick, 1 min, 2 mins, 3 mins, 5 mins, 10 mins, 15 mins,
  30 mins, 60 mins, eod, week, month` (Python strips spaces/'s' → the
  same wire tokens + `week`, `month`).
- **`bidask=1`** on tick queries adds bid/ask columns ("Historical Data
  is also available with & without (Default) Bid Ask History" — KB via
  search-relay).
- **`delivery=true`** (EOD bars only) — delivery-quantity column
  (pkg-source + the vendor sample repo, high).
- **`comp=true`** — LZ4-block-compressed response (8-byte header:
  uncompressed len + compressed len, little-endian;
  `lz4.block.decompress`) — Python SDK v6.0.5+ (pkg-source, high).
- **`getSymbolId=1`** (Node) — resolves the symbol id via
  `api.truedata.in/getAllSymbols?…&search=<symbol>` alongside the call.

## 3. Response / CSV columns & error strings

- Node SDK csv colParser keys (the columns it expects — pkg-source,
  high): `ltp, volume, oi, bid, bidqty, ask, askqty, open, high, low,
  close, vol, dopen, dhigh, dlow, dclose, turnover, pclose, change,
  changeper, symbolid` (+ timestamp/time string columns implied).
- JSON envelope (Node SDK builds):
  `{ Records: […], status: 'Success', symbol | segment, symbolId? }`.
- Error status strings returned as body text (pkg-source, high):
  `No data exists...` · `Symbol does not exist...` ·
  `Invalid parameter value...`; auth failure:
  `Message: 'Authorization has been denied for this request'`;
  rate limit: body `'API request quota exceeded!'` (the Node SDK staggers
  1 s and retries). Analytics/greeks 429 responses carry a
  **base64-encoded** error body (Python SDK decodes; pkg-source, high).
- HONEST caveat: these response shapes are reconstructed from SDK PARSING
  code — no raw wire bytes were captured (the official PDF + a live probe
  are the cross-check).

## 4. Rate limits (REST) — KB "Request/Call Limit…", search-relay (medium; identical across two independent relays)

| Data | Per second | Per minute | Per hour |
|---|---|---|---|
| Tick data | 5 | 300 | 18,000 |
| Minute-bar data | 10 | 600 | 18,000 |

No daily cap statement was retrieved (UNVERIFIED whether one exists).

## 5. Historical DEPTH (the BruteX-critical section)

Per the vendor KB article "Historical Data Availability Through REST API"
(search-relay, medium; tick 5-day also in the npm README = high):

| Data | Default depth |
|---|---|
| Tick | last **5 trading days** ("Max upto last 5 days of tick data") |
| Intraday bars (1–60 min) | last **6 months** |
| Daily (EOD) bars | **10+ years** |

- **"Extended History is being made available as an Add-on in case you
  would need more history."** — longer 1-min/tick history is a PAID
  ADD-ON; the actually-purchasable depth (e.g. 1-min back to which year)
  is **UNVERIFIED** — THE key sales/probe question for BruteX (whose
  Groww floor is 2020-01-01 at 1-minute, CLAUDE.md §6).
- Velocity (desktop product) marketing mentions "up to 20 days of tick
  data storage for over 50,000 symbols" (search-relay, medium) —
  product-side, not the API default.
- Per-segment depth differences (NSE EQ vs F&O vs MCX vs BSE),
  expired-contract history availability, and continuous-futures history
  depth: **UNVERIFIED**.

## 6. Per-request span / pagination

No explicit per-request date-span cap or pagination scheme appears in the
SDK code (arbitrary from/to; no page tokens). npm README caps only the
DEFAULTS (`nticks=2000`, `nbars=200`, both "Any number"). Whether the
server caps a single request's span beyond the depth entitlements:
**UNVERIFIED** — pin in a live probe before chunk-planning an adapter.

## 7. Segments (for bhavcopy / gainers endpoints)

- Bhavcopy `segment`: `FO (default), EQ, MCX, BSEFO, BSEEQ` (npm README,
  direct-fetch, high).
- Gainers/losers `topsegment`: `NSEFUT (default), NSEEQ, NSEOPT, CDS,
  MCX` (npm README, direct-fetch, high).
- Full-feed prev-close/OI cache segments: `EQ, FO, MCX, CDS, IN, BSEEQ,
  BSEFO` (`IN` ≈ indices — inference, low) (pkg-source).

## 8. Notable behaviors

- `getLTP` (Node) = `/getlastnticks?…&nticks=1&interval=tick` — LTP is
  just the newest tick (pkg-source, high). Dedicated LTP endpoints also
  exist on the analytics host ([05](./05-analytics-corporate.md)).
- Bhavcopy date default = current date (`yyyy-mm-dd`); the Python SDK
  checks `/getbhavcopystatus` first and errors "No complete bhavcopy
  found for requested date. Last available for <ts>" (pkg-source, high).
- Legacy: history was once served over WebSocket; deprecated ~2021-04-30
  (Scribd doc summary via search-relay + truedata-ws v3.0.1 changelog;
  medium/high).

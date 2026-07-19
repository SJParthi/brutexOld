# Groww Trading API — Complete Offline Documentation

> Mirror of https://groww.in/trade-api/docs (both the cURL and Python SDK variants) plus https://pypi.org/project/growwapi/.
> Captured: 2026-07-15. See [SOURCES.md](./SOURCES.md) for per-URL capture status.

**Base URL:** `https://api.groww.in` — all REST endpoints below are relative to it.
**Required headers:** `Authorization: Bearer {ACCESS_TOKEN}`, `Accept: application/json`, `X-API-VERSION: 1.0`.
**Instrument master CSV:** https://growwapi-assets.groww.in/instruments/instrument.csv
**Python SDK:** `pip install growwapi` (v1.5.0, Python 3.9+, MIT).

## Files

| File | Contents |
| --- | --- |
| [01-introduction.md](./01-introduction.md) | Getting started, prerequisites, subscription, request/response structure, headers, success/failure envelope, error codes + rate-limit tables from the intro pages |
| [02-authentication.md](./02-authentication.md) | All 3 auth flows (daily Access Token; API key+secret approval flow; TOTP flow), `POST /v1/token/api/access` schemas, checksum generation in Python/Java/.NET/JavaScript |
| [03-instruments.md](./03-instruments.md) | Instrument CSV download URL, all 19 columns, segments, SDK lookup methods (`get_instrument_by_*`, `get_all_instruments`) |
| [04-orders.md](./04-orders.md) | Place / Modify / Cancel / Trades-for-order / Status / Status-by-reference / Order list / Order detail — full schemas, curl + python for each |
| [05-smart-orders.md](./05-smart-orders.md) | GTT & OCO: create/modify/cancel/get/list (`/v1/order-advance/*`), modifiable-fields matrix, SDK constants, full GTT/OCO response schemas |
| [06-portfolio.md](./06-portfolio.md) | Holdings (`/v1/holdings/user`), positions (`/v1/positions/user`, `/v1/positions/trading-symbol`) |
| [07-margin.md](./07-margin.md) | User margin (`/v1/margins/detail/user`) incl. FNO/equity/commodity blocks; required margin for order/basket (`/v1/margins/detail/orders`) |
| [08-live-data.md](./08-live-data.md) | Quote, LTP (50 max), OHLC (50 max), Option Chain with Greeks, Greeks endpoint |
| [09-historical-data.md](./09-historical-data.md) | DEPRECATED `/v1/historical/candle/range` + its interval/duration table |
| [10-backtesting.md](./10-backtesting.md) | Groww symbol format, Expiries, Contracts, current candles endpoint `/v1/historical/candles` (data from 2020, OI for FNO), data limits |
| [11-websocket-feed.md](./11-websocket-feed.md) | `GrowwFeed` streaming (NATS/protobuf under the hood): LTP, index value, market depth, FNO/equity order updates, FNO position updates, metadata; all message formats |
| [12-user.md](./12-user.md) | User profile `/v1/user/detail` (UCC, exchanges, segments, DDPI) |
| [13-annexures.md](./13-annexures.md) | Every enum: order status, AMO status, exchange, segment, order type, product, transaction type, validity, candle interval, instrument type + SDK constants |
| [14-python-sdk.md](./14-python-sdk.md) | PyPI metadata/releases/hashes, README examples, complete `GrowwAPI` + `GrowwFeed` method surface mapped to REST endpoints |
| [15-rate-limits.md](./15-rate-limits.md) | Rate-limit table (Orders 10/s 250/min; Live Data 10/s 300/min; Non-Trading 20/s 500/min) + every other numeric limit in the docs |
| [16-errors-exceptions.md](./16-errors-exceptions.md) | FAILURE envelope, GA000–GA007 error codes, all `growwapi.groww.exceptions` classes with attributes |
| [17-changelog.md](./17-changelog.md) | API changelog (v1.0 releases, version policy: partial semver, 6-month sunset) + full Python SDK changelog 0.0.1→1.5.0 |

## REST endpoint quick reference

| Method | Path | Purpose | File |
| --- | --- | --- | --- |
| POST | `/v1/token/api/access` | Generate access token (approval/totp) | 02 |
| GET | `https://growwapi-assets.groww.in/instruments/instrument.csv` | Instrument master | 03 |
| POST | `/v1/order/create` | Place order | 04 |
| POST | `/v1/order/modify` | Modify order | 04 |
| POST | `/v1/order/cancel` | Cancel order | 04 |
| GET | `/v1/order/trades/{groww_order_id}` | Trades for order | 04 |
| GET | `/v1/order/status/{groww_order_id}` | Order status | 04 |
| GET | `/v1/order/status/reference/{order_reference_id}` | Status by reference | 04 |
| GET | `/v1/order/list` | Order list (day) | 04 |
| GET | `/v1/order/detail/{groww_order_id}` | Order detail | 04 |
| POST | `/v1/order-advance/create` | Create GTT/OCO | 05 |
| PUT | `/v1/order-advance/modify/{smart_order_id}` | Modify smart order | 05 |
| POST | `/v1/order-advance/cancel/{segment}/{type}/{id}` | Cancel smart order | 05 |
| GET | `/v1/order-advance/status/{segment}/{type}/internal/{id}` | Get smart order | 05 |
| GET | `/v1/order-advance/list` | List smart orders | 05 |
| GET | `/v1/holdings/user` | Holdings | 06 |
| GET | `/v1/positions/user` | Positions (all/segment) | 06 |
| GET | `/v1/positions/trading-symbol` | Position for symbol | 06 |
| GET | `/v1/margins/detail/user` | Available margin | 07 |
| POST | `/v1/margins/detail/orders?segment=` | Required margin (basket) | 07 |
| GET | `/v1/live-data/quote` | Full quote | 08 |
| GET | `/v1/live-data/ltp` | LTP (≤50 symbols) | 08 |
| GET | `/v1/live-data/ohlc` | OHLC snapshot (≤50) | 08 |
| GET | `/v1/option-chain/exchange/{ex}/underlying/{u}?expiry_date=` | Option chain + Greeks | 08 |
| GET | `/v1/live-data/greeks/exchange/{ex}/underlying/{u}/trading_symbol/{ts}/expiry/{d}` | Greeks | 08 |
| GET | `/v1/historical/candle/range` | DEPRECATED candles | 09 |
| GET | `/v1/historical/expiries` | FNO expiries (2020+) | 10 |
| GET | `/v1/historical/contracts` | FNO contracts for expiry | 10 |
| GET | `/v1/historical/candles` | Historical candles (current) | 10 |
| GET | `/v1/user/detail` | User profile | 12 |
| (SDK) | `GrowwFeed` streaming | LTP/index/depth/order/position streams | 11 |

## Key enums at a glance

- exchange: `NSE` `BSE` `MCX` · segment: `CASH` `FNO` `COMMODITY` · product: `CNC` `MIS` `NRML`
- order_type: `LIMIT` `MARKET` `SL` `SL_M` · transaction_type: `BUY` `SELL` · validity: `DAY`
- order_status: `NEW` `ACKED` `TRIGGER_PENDING` `APPROVED` `REJECTED` `FAILED` `EXECUTED` `DELIVERY_AWAITED` `CANCELLED` `CANCELLATION_REQUESTED` `MODIFICATION_REQUESTED` `COMPLETED`
- amo_status: `NA` `PENDING` `DISPATCHED` `PARKED` `PLACED` `FAILED` `MARKET`
- candle_interval: `1minute` `2minute` `3minute` `5minute` `10minute` `15minute` `30minute` `1hour` `4hour` `1day` `1week` `1month`
- instrument_type: `EQ` `IDX` `FUT` `CE` `PE` · smart orders: `GTT` `OCO`; trigger: `UP` `DOWN`

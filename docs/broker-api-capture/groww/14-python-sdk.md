# growwapi — Python SDK Package Reference

> Sources:
> - https://pypi.org/project/growwapi/ (PyPI listing, latest 1.5.0, released Dec 6, 2025)
> - Method surface compiled from all pages under https://groww.in/trade-api/docs/python-sdk
> Captured: 2026-07-15

## Package metadata (PyPI)

| Field | Value |
| --- | --- |
| Package | `growwapi` |
| Latest version | **1.5.0** (Released: Dec 6, 2025) |
| Description | The foundational SDK for accessing Groww APIs and listening to live data streams. This package provides the core functionalities required to interact with Groww's trading platform. |
| License | MIT License (MIT) |
| Author | Groww (growwapi@groww.in) |
| Requires | Python `<4.0, >=3.9` |
| Classifiers | Python 3, 3.9, 3.10, 3.11, 3.12, 3.13 |
| Maintainer (PyPI) | growwapi |
| Install | `pip install growwapi` |
| Docs link | https://groww.in/trade-api/docs/python-sdk |

### Release history on PyPI

| Version | Date |
| --- | --- |
| 1.5.0 | Dec 6, 2025 |
| 1.3.0 | Nov 26, 2025 |
| 1.2.0 | Nov 10, 2025 |
| 1.1.0 | Oct 8, 2025 |
| 1.0.0 | Oct 3, 2025 |
| 0.0.9 | Sep 10, 2025 |
| 0.0.8 | Jun 7, 2025 |
| 0.0.7 | May 21, 2025 |
| 0.0.6 | May 2, 2025 |
| 0.0.5 | Apr 18, 2025 |
| 0.0.4 | Apr 11, 2025 |

(Note: 1.4.0 appears in the docs changelog but not on the PyPI visible release list; 0.0.1–0.0.3 have been removed from the PyPI listing.)

### Distribution files (1.5.0)

| File | Size | SHA256 |
| --- | --- | --- |
| growwapi-1.5.0.tar.gz (source) | 69.1 kB | `546be8517f1223dda1f5338a549b3499c2b7cabd1e165b64fef21ab386136b66` |
| growwapi-1.5.0-py3-none-any.whl | 76.5 kB | `b4871ed19415b6ad7434bee2178f53aa1e180fbddd4a25693c458440f66939cc` |

## PyPI Features list (verbatim)

- Connect to Groww's API
- Place, modify, and cancel orders
- Retrieve order details, status, holdings, trade lists
- Subscribe and unsubscribe to market feeds
- Get live market data and updates

## PyPI README usage examples (verbatim)

### Authentication

To use the SDK, you need to authenticate using your API credentials. Set the following variables:

- `API_AUTH_TOKEN`: Your API authentication token.

### API Client

The `GrowwAPI` class provides methods to interact with the Groww API.

```python
from growwapi import GrowwAPI

groww_client = GrowwAPI("YOUR_API_KEY")

# Get the current orders. Will wait for 5 seconds or until the orders are received
orders = groww_client.get_order_list(timeout=5)
print(orders)
```

### Feed Client

The `GrowwFeed` class provides methods to subscribe to and receive Groww data streams and updates. It can either be used synchronously to get the last updated data or asynchronously to trigger a callback whenever new data is received.

```python
from growwapi import GrowwFeed
from growwapi import GrowwAPI

groww_feed = GrowwFeed("YOUR_API_KEY")

# Synchronous Usage: Create a subscription and then get the LTP
groww_feed.subscribe_live_data(GrowwAPI.SEGMENT_CASH, "SWIGGY")
# Will wait for 3 seconds or until the LTP is received
ltp = groww_feed.get_stocks_ltp("SWIGGY", timeout=3)
print(ltp)


# Asynchronous Usage: Callback triggered whenever the LTP changes
def get_ltp_print():
  # As it was triggerred on data received, we can directly get the LTP
  ltp = groww_feed.get_stocks_ltp("RELIANCE")
  print(ltp)


groww_feed.subscribe_live_data(GrowwAPI.SEGMENT_CASH, "RELIANCE", on_data_received=get_ltp_print)
```

> Caution: the PyPI README examples above (`subscribe_live_data`, `get_stocks_ltp`, constructing `GrowwFeed("YOUR_API_KEY")`) reflect an **older API surface**. The current docs (see [11-websocket-feed.md](./11-websocket-feed.md)) construct the feed as `GrowwFeed(GrowwAPI(token))` and use `subscribe_ltp`/`get_ltp` with instrument dicts. The docs changelog notes `get_latest_price_data` was renamed to `get_quote` and `GrowwClient` to `GrowwAPI` in 0.0.3.

---

# Complete GrowwAPI method surface (compiled from official docs)

Class: `growwapi.GrowwAPI`

## Construction & auth (static/class methods)

| Method | Purpose |
| --- | --- |
| `GrowwAPI(access_token)` | Initialize the client with an access token |
| `GrowwAPI.get_access_token(api_key=..., secret=...)` | Generate daily access token from API key + secret (checksum flow) |
| `GrowwAPI.get_access_token(api_key=..., totp=...)` | Generate access token from TOTP token + code |

## Instruments

| Method | Purpose |
| --- | --- |
| `get_instrument_by_groww_symbol(groww_symbol)` | Instrument details by Groww symbol (e.g. `NSE-RELIANCE`) |
| `get_instrument_by_exchange_and_trading_symbol(exchange, trading_symbol)` | Instrument details by exchange + trading symbol |
| `get_instrument_by_exchange_token(exchange_token)` | Instrument details by exchange token |
| `get_all_instruments()` | All instruments as a pandas DataFrame |
| `_load_instruments()` | (internal) load instrument CSV as DataFrame |
| `INSTRUMENT_CSV_URL` | Constant: instrument CSV download URL |

## Orders

| Method | Purpose |
| --- | --- |
| `place_order(trading_symbol, quantity, validity, exchange, segment, product, order_type, transaction_type, price=None, trigger_price=None, order_reference_id=None)` | Place order (`POST /v1/order/create`) |
| `modify_order(quantity, order_type, segment, groww_order_id, price=None, trigger_price=None)` | Modify order (`POST /v1/order/modify`) |
| `cancel_order(segment, groww_order_id)` | Cancel order (`POST /v1/order/cancel`) |
| `get_trade_list_for_order(groww_order_id, segment, page=0, page_size=50)` | Trades for an order (`GET /v1/order/trades/{id}`) |
| `get_order_status(groww_order_id, segment)` | Order status (`GET /v1/order/status/{id}`) |
| `get_order_status_by_reference(order_reference_id, segment)` | Order status by user reference (`GET /v1/order/status/reference/{ref}`) |
| `get_order_list(segment=None, page=0, page_size=100)` | Day's orders (`GET /v1/order/list`) |
| `get_order_detail(groww_order_id, segment)` | Full order detail (`GET /v1/order/detail/{id}`) |

## Smart Orders (GTT / OCO)

| Method | Purpose |
| --- | --- |
| `create_smart_order(smart_order_type, reference_id, segment, trading_symbol, quantity, product_type, exchange, duration, trigger_price=..., trigger_direction=..., order={...}, net_position_quantity=..., transaction_type=..., target={...}, stop_loss={...}, child_legs=...)` | Create GTT or OCO (`POST /v1/order-advance/create`) |
| `modify_smart_order(smart_order_id, smart_order_type, segment, ...)` | Modify GTT/OCO (`PUT /v1/order-advance/modify/{id}`) |
| `cancel_smart_order(segment, smart_order_type, smart_order_id)` | Cancel (`POST /v1/order-advance/cancel/{segment}/{type}/{id}`) |
| `get_smart_order(segment, smart_order_type, smart_order_id)` | Get one (`GET /v1/order-advance/status/{segment}/{type}/internal/{id}`) |
| `get_smart_order_list(segment=..., smart_order_type=..., status=..., page=..., page_size=..., start_date_time=..., end_date_time=...)` | List (`GET /v1/order-advance/list`) |

## Portfolio

| Method | Purpose |
| --- | --- |
| `get_holdings_for_user(timeout=None)` | DEMAT holdings (`GET /v1/holdings/user`) |
| `get_positions_for_user(segment=None)` | All positions (`GET /v1/positions/user`) |
| `get_position_for_trading_symbol(trading_symbol, segment)` | Position for a symbol (`GET /v1/positions/trading-symbol`) |

## Margin

| Method | Purpose |
| --- | --- |
| `get_available_margin_details()` | User margin (`GET /v1/margins/detail/user`) |
| `get_order_margin_details(segment, orders)` | Required margin for order/basket (`POST /v1/margins/detail/orders`) |

## Live Data

| Method | Purpose |
| --- | --- |
| `get_quote(exchange, segment, trading_symbol)` | Full quote snapshot (`GET /v1/live-data/quote`) |
| `get_ltp(segment, exchange_trading_symbols)` | LTP for up to 50 instruments (`GET /v1/live-data/ltp`) |
| `get_ohlc(segment, exchange_trading_symbols)` | Real-time OHLC for up to 50 instruments (`GET /v1/live-data/ohlc`) |
| `get_option_chain(exchange, underlying, expiry_date)` | Option chain incl. Greeks (`GET /v1/option-chain/...`) |
| `get_greeks(exchange, underlying, trading_symbol, expiry)` | Greeks for a contract (`GET /v1/live-data/greeks/...`) |

## Historical / Backtesting

| Method | Purpose |
| --- | --- |
| `get_historical_candle_data(trading_symbol, exchange, segment, start_time, end_time, interval_in_minutes=...)` | DEPRECATED — old candle range API (`GET /v1/historical/candle/range`) |
| `get_expiries(exchange, underlying_symbol, year=None, month=None)` | FNO expiry dates (`GET /v1/historical/expiries`) |
| `get_contracts(exchange, underlying_symbol, expiry_date)` | Contract Groww symbols for an expiry (`GET /v1/historical/contracts`) |
| `get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` | Candle data with OI (`GET /v1/historical/candles`) |

## User

| Method | Purpose |
| --- | --- |
| `get_user_profile()` | User profile (`GET /v1/user/detail`) |

## Constants on GrowwAPI

See [13-annexures.md](./13-annexures.md) for the complete constant tables:
`EXCHANGE_NSE`, `EXCHANGE_BSE`, `EXCHANGE_MCX`; `SEGMENT_CASH`, `SEGMENT_FNO`, `SEGMENT_COMMODITY`; `ORDER_TYPE_LIMIT`, `ORDER_TYPE_MARKET`, `ORDER_TYPE_STOP_LOSS`, `ORDER_TYPE_STOP_LOSS_MARKET`; `PRODUCT_CNC`, `PRODUCT_MIS`, `PRODUCT_NRML`; `TRANSACTION_TYPE_BUY`, `TRANSACTION_TYPE_SELL`; `VALIDITY_DAY`; `CANDLE_INTERVAL_MIN_1/2/3/5/10/15/30`, `CANDLE_INTERVAL_HOUR_1/4`, `CANDLE_INTERVAL_DAY/WEEK/MONTH`; `SMART_ORDER_TYPE_GTT/OCO`; `TRIGGER_DIRECTION_UP/DOWN`; `SMART_ORDER_STATUS_ACTIVE/TRIGGERED/CANCELLED/EXPIRED/FAILED/COMPLETED`; `INSTRUMENT_CSV_URL`.

---

# Complete GrowwFeed method surface

Class: `growwapi.GrowwFeed` — construct with `GrowwFeed(GrowwAPI(token))`. Transport: NATS-based WebSocket streaming with protobuf message parsing (per the SDK changelog: "NATS ping handling", "Protobuf response parsing", "Live Market Data via NATS WebSocket"). Up to 1000 subscriptions at a time.

| Method | Purpose |
| --- | --- |
| `subscribe_ltp(instruments_list, on_data_received=cb)` | Subscribe LTP for equity/derivative instruments (list of `{"exchange","segment","exchange_token"}` dicts) |
| `get_ltp()` | Get last received LTP data (sync) |
| `unsubscribe_ltp(instruments_list)` | Unsubscribe LTP |
| `subscribe_index_value(instruments_list, on_data_received=cb)` | Subscribe index values |
| `get_index_value()` | Get last received index values |
| `unsubscribe_index_value(instruments_list)` | Unsubscribe index values |
| `subscribe_market_depth(instruments_list, on_data_received=cb)` | Subscribe 5-level market depth |
| `get_market_depth()` | Get last received depth books |
| `unsubscribe_market_depth(instruments_list)` | Unsubscribe depth |
| `subscribe_fno_order_updates(on_data_received=cb)` | Subscribe derivatives order updates |
| `get_fno_order_update()` | Get last FNO order update |
| `unsubscribe_fno_order_updates()` | Unsubscribe |
| `subscribe_equity_order_updates(on_data_received=cb)` | Subscribe equity order updates |
| `get_equity_order_update()` | Get last equity order update |
| `unsubscribe_equity_order_updates()` | Unsubscribe |
| `subscribe_fno_position_updates(on_data_received=cb)` | Subscribe derivatives position updates |
| `get_fno_position_update()` | Get last FNO position update |
| `unsubscribe_fno_position_updates()` | Unsubscribe |
| `consume()` | Blocking consume loop ("Nothing after this will run") |

Callback signature: `def on_data_received(meta): ...` where `meta` contains `exchange`, `segment`, `feed_type` (e.g. `ltp`, `order_updates`, `position_updates`), and `feed_key` (exchange token). Full message formats: [11-websocket-feed.md](./11-websocket-feed.md).

## Exceptions module

`growwapi.groww.exceptions` — see [16-errors-exceptions.md](./16-errors-exceptions.md) for all classes: `GrowwBaseException`, `GrowwAPIException` (+ `GrowwAPIAuthenticationException`, `GrowwAPIAuthorisationException`, `GrowwAPIBadRequestException`, `GrowwAPINotFoundException`, `GrowwAPIRateLimitException`, `GrowwAPITimeoutException`), `GrowwFeedException`, `GrowwFeedConnectionException`, `GrowwFeedNotSubscribedException`.

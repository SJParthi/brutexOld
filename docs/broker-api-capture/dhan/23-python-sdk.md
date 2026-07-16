# DhanHQ Python SDK (dhanhq) — REST Client

> Sources:
> - https://pypi.org/project/dhanhq/ (v2.2.0, released Apr 24, 2026, MIT License, author Dhan <dhan-oss@dhan.co>)
> - https://raw.githubusercontent.com/dhan-oss/DhanHQ-py/main/README.md
> - https://dhanhq.co/docs/DhanHQ-py/ (module reference pages: dhanhq, dhan_context, dhan_http, init, _order, _super_order, _forever_order, _funds, _portfolio, _security, _statement, _trader_control, _historical_data, _market_feed, _option_chain)
> Captured: 2026-07-15
>
> WebSocket helper classes (MarketFeed, OrderUpdate, FullDepth) are documented in [24-python-sdk-websockets.md](./24-python-sdk-websockets.md).

# DhanHQ-py : v2.2.0

> With the release of v2.2.0, there are breaking changes. You can install previous stable version by using the below code:

```bash
pip install dhanhq==2.0.2
```

The official Python client for communicating with the [Dhan API](https://api.dhan.co/v2/).

DhanHQ-py Rest API is used to automate investing and trading. Execute orders in real time along with position management, live and historical data, tradebook and more with simple API collection. Not just this, you also get real-time market data via DhanHQ Live Market Feed.

Dhan (c) 2026. Licensed under the MIT License.

**Documentation links**
- DhanHQ Python Documentation: https://dhanhq.co/docs/DhanHQ-py/
- DhanHQ Developer Kit: https://api.dhan.co/v2/
- DhanHQ API Documentation: https://dhanhq.co/docs/v2/

**Release history (PyPI)**: 2.2.0 (Apr 24, 2026) · 2.2.0rc1 (Jan 2, 2026) · 2.1.0 (Mar 12, 2025, **yanked** — breaking changes) · 2.0.2 (Nov 29, 2024) · 2.0.1 (Nov 7, 2024) · 2.0.0 (Oct 25, 2024) · 1.3.3 (Oct 1, 2024) · 1.3.2 (Aug 22, 2024) · 1.3 (Feb 1, 2024) · 1.2.3 (Dec 27, 2023) · 1.2.2 (Mar 29, 2023) · 1.2.1 (Mar 9, 2023) · 1.2 (Feb 2, 2023) · 1.1 (Oct 11, 2022) · 1.0 (Sep 1, 2022)

## v2.2.0 - What's new

- You can now access the entire full market depth (200 Level) via DhanHQ APIs and part of the python library.
- Expired Options Data are now directly available on the library and you can fetch for all NSE & BSE instruments
- Super Orders - smart orders for managing risk and profits is now introduced with DhanHQ
- You can set, modify and change IP for your account, right from the python library - the code for the same is available under example.

## v2.1.0 - What's new

- 20 level market depth is now available on DhanHQ APIs and part of the python library.
- The project is restructured to contemporary "best practices" in the python world. Import changes:

| Before This Version                         | After This Release             |
| ------------------------------------------- | ------------------------------ |
| from dhanhq import dhanhq                   | from dhanhq import dhanhq      |
| from dhanhq import marketfeed.MarketFeed    | from dhanhq import MarketFeed  |
| from dhanhq import orderupdate.OrderUpdate  | from dhanhq import OrderUpdate |

- Constants moved from modules to classes:

| Before This Version | After This Release |
| ------------------- | ------------------ |
| marketfeed.NSE      | MarketFeed.NSE     |
| marketfeed.Ticker   | MarketFeed.Ticker  |

- Credentials via `DhanContext` (defined once, passed to all SDK classes):

| Before This Version                                  | After This Release                       |
| ---------------------------------------------------- | ---------------------------------------- |
| `dhanhq('client_id','access_token')`                 | `dhanhq('dhan_context')`                 |
| `MarketFeed('client_id','access_token',instruments)` | `MarketFeed('dhan_context',instruments)` |
| `OrderUpdate('client_id','access_token')`            | `OrderUpdate('dhan_context')`            |

## Features

- **Order Management** — place, cancel, modify pending orders; retrieve order status, trade status, order book & tradebook.
- **Live Market Feed** — real-time market data across exchanges.
- **Market Quote** — REST snapshot: ticker mode, quote mode or full mode.
- **Option Chain** — entire Option Chain: OI, greeks, volume, top bid/ask, price data.
- **Forever Order** — place, modify, delete Forever Orders (single or OCO).
- **Portfolio Management** — retrieve/manage holdings and positions.
- **Historical Data** — minute-timeframe and daily OHLC candles.
- **Fund Details** — balance, margin utilised, collateral, and margin required per order.
- **eDIS Authorisation** — CDSL eDIS flow, T-PIN generation and stock marking.

## Quickstart

```bash
pip install dhanhq
```

### Authentication (`DhanLogin` class)

#### Method 1: OAuth Flow

```python
from dhanhq import DhanLogin

dhan_login = DhanLogin("YOUR_CLIENT_ID")
app_id = "YOUR_APP_ID"
app_secret = "YOUR_APP_SECRET"

# Step 1: Generate Consent and Open Browser for Login
consent_id = dhan_login.generate_login_session(app_id, app_secret)

# Step 2: Consume Token ID (After user logs in and gets Token ID from redirect URL)
token_id = "TOKEN_ID_FROM_REDIRECT_URL"
access_token = dhan_login.consume_token_id(token_id, app_id, app_secret)
print(access_token)
```

#### Method 2: PIN & TOTP Flow

```python
from dhanhq import DhanLogin

dhan_login = DhanLogin("YOUR_CLIENT_ID")
pin = "YOUR_PIN"
totp = "YOUR_TOTP"

access_token_data = dhan_login.generate_token(pin, totp)
print(access_token_data)
```

#### Renew Token

```python
dhan_login.renew_token(access_token)
```

#### User Profile

```python
# Check validity of access token and account setup
user_info = dhan_login.user_profile(access_token)
print(user_info)
```

### IP Management (`DhanLogin`)

You can manage your Static IP (whitelisting) using `set_ip`, `modify_ip`, and `get_ip`.

```python
# Set Primary IP
response = dhan_login.set_ip(access_token, "10.200.10.10", "PRIMARY")
print(response)
# Modify Primary IP
response = dhan_login.modify_ip(access_token, "10.200.10.11", "PRIMARY")
print(response)
# Get Configured IPs
ip_list = dhan_login.get_ip(access_token)
print(ip_list)
```

### Hands-on API (main client)

```python
from dhanhq import DhanContext, dhanhq

dhan_context = DhanContext("client_id","access_token")
dhan = dhanhq(dhan_context)

# Place an order for Equity Cash
dhan.place_order(security_id='1333',            # HDFC Bank
    exchange_segment=dhan.NSE,
    transaction_type=dhan.BUY,
    quantity=10,
    order_type=dhan.MARKET,
    product_type=dhan.INTRA,
    price=0)

# Place an order for NSE Futures & Options
dhan.place_order(security_id='52175',           # Nifty PE
    exchange_segment=dhan.NSE_FNO,
    transaction_type=dhan.BUY,
    quantity=550,
    order_type=dhan.MARKET,
    product_type=dhan.INTRA,
    price=0)

# Fetch all orders
dhan.get_order_list()

# Get order by id
dhan.get_order_by_id(order_id)

# Modify order
dhan.modify_order(order_id, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)

# Cancel order
dhan.cancel_order(order_id)

# Get order by correlation id
dhan.get_order_by_correlationID(correlationID)

# Get Instrument List
dhan.fetch_security_list("compact")

# Get positions
dhan.get_positions()

# Get holdings
dhan.get_holdings()

# Intraday Minute Data
dhan.intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date)

# Historical Daily Data
dhan.historical_daily_data(security_id, exchange_segment, instrument_type, from_date, to_date)

# Expired Options Data
dhan.expired_options_data(
    security_id=13,
    exchange_segment="NSE_FNO",
    instrument_type="INDEX",
    expiry_flag="MONTH",
    expiry_code=1,
    strike="ATM",
    drv_option_type="CALL",
    required_data=["open", "high", "low", "close", "volume"],
    from_date="2023-01-01",
    to_date="2023-01-31"
)

# Time Converter
dhan.convert_to_date_time(epoch_date)

# Get trade book
dhan.get_trade_book(order_id)

# Get trade history
dhan.get_trade_history(from_date,to_date,page_number=0)

# Get fund limits
dhan.get_fund_limits()

# Generate TPIN
dhan.generate_tpin()

# Enter TPIN in Form
dhan.open_browser_for_tpin(isin='INE00IN01015',
    qty=1,
    exchange='NSE')

# EDIS Status and Inquiry
dhan.edis_inquiry(isin='INE00IN01015')

# Expiry List of Underlying
dhan.expiry_list(
    under_security_id=13,                       # Nifty
    under_exchange_segment="IDX_I"
)

# Option Chain
dhan.option_chain(
    under_security_id=13,                       # Nifty
    under_exchange_segment="IDX_I",
    expiry="2024-10-31"
)

# Market Quote Data                     # LTP - ticker_data, OHLC - ohlc_data, Full Packet - quote_data
dhan.ohlc_data(
    securities = {"NSE_EQ":[1333]}
)

# Place Forever Order (SINGLE)
dhan.place_forever(
    security_id="1333",
    exchange_segment= dhan.NSE,
    transaction_type= dhan.BUY,
    order_type=dhan.LIMIT,
    product_type=dhan.CNC,
    quantity= 10,
    price= 1900,
    trigger_Price= 1950
)
```

---

# Class Reference

## `dhanhq` (facade class) — `dhanhq.dhanhq`

```python
dhanhq(dhan_context)
```

Bases: `Order`, `ForeverOrder`, `Portfolio`, `Funds`, `Statement`, `TraderControl`, `Security`, `MarketFeed` (REST market quote mixin), `HistoricalData`, `OptionChain`, `SuperOrder`.

DhanHQ Class having Core APIs. All methods of the mixin classes below are available directly on a `dhanhq` instance.

**Class constants** (documented on the reference site; sample values shown):

| Constant | Value               | Group                          |
| -------- | ------------------- | ------------------------------ |
| `INDEX`  | `'IDX_I'`           | Exchange segment constants (also `NSE`, `BSE`, `NSE_FNO`, `CUR`, `MCX`, etc.) |
| `MTF`    | `'MTF'`             | Product type constants (also `CNC`, `INTRA`, `MARGIN`, `CO`, `BO`) |
| `SELL`   | `'SELL'`            | Transaction type constants (also `BUY`) |
| `SLM`    | `'STOP_LOSS_MARKET'`| Order type constants (also `LIMIT`, `MARKET`, `SL`) |

**Static method**

### `convert_to_date_time(epoch)` (staticmethod)

Convert EPOCH time to Python datetime object in IST.

| Name    | Type  | Description                | Default    |
| ------- | ----- | -------------------------- | ---------- |
| `epoch` | `int` | The EPOCH time to convert. | *required* |

Returns: `datetime` — Corresponding datetime object in IST.

## `DhanContext` — `dhanhq.dhan_context`

```python
DhanContext(client_id, access_token, disable_ssl=False, pool=None)
```

A class that encapsulates connection context to Dhan APIs like client-id, access-token, base-url and passes this to all the connection protocols like http and websocket that it is composed of.

| Method               | Description                                                                    |
| -------------------- | ------------------------------------------------------------------------------ |
| `get_access_token()` | Return authorization token used for connecting to Dhan API.                   |
| `get_client_id()`    | Return client's id used to identify the client interacting with Dhan API.     |
| `get_dhan_http()`    | Return HTTP Connection Request object (`DhanHTTP`) with all necessary context.|

## `DhanHTTP` — `dhanhq.dhan_http`

```python
DhanHTTP(client_id, access_token, disable_ssl=False, pool=None)
```

Manages API keys, connection context, and HTTP requests. Inner enums: `HttpMethods` (constants for HTTP requests), `HttpResponseStatus` (constants for HTTP status codes).

| Method                    | Description                                                       |
| ------------------------- | ----------------------------------------------------------------- |
| `get(endpoint)`           | HTTP-GET to Dhan endpoint (path without base URL). Returns dict.  |
| `post(endpoint, payload)` | HTTP-POST with dict payload. Returns dict.                        |
| `put(endpoint, payload)`  | HTTP-PUT with dict payload. Returns dict.                         |
| `delete(endpoint)`        | HTTP-DELETE. Returns dict.                                        |

---

## `Order` — `dhanhq._order`

```python
Order(dhan_context)
```

### `place_order(...)`

```python
place_order(security_id, exchange_segment, transaction_type, quantity, order_type,
            product_type, price, trigger_price=0, disclosed_quantity=0,
            after_market_order=False, validity='DAY', amo_time='OPEN',
            bo_profit_value=None, bo_stop_loss_Value=None, tag=None, should_slice=False)
```

Place a new order in the Dhan account.

| Name                 | Type    | Description                                 | Default    |
| -------------------- | ------- | ------------------------------------------- | ---------- |
| `security_id`        | `str`   | The ID of the security to trade.            | *required* |
| `exchange_segment`   | `str`   | The exchange segment (e.g., NSE, BSE).      | *required* |
| `transaction_type`   | `str`   | The type of transaction (BUY/SELL).         | *required* |
| `quantity`           | `int`   | The quantity of the order.                  | *required* |
| `order_type`         | `str`   | The type of order (LIMIT, MARKET, etc.).    | *required* |
| `product_type`       | `str`   | The product type (CNC, INTRA, etc.).        | *required* |
| `price`              | `float` | The price of the order.                     | *required* |
| `trigger_price`      | `float` | The trigger price for the order.            | `0`        |
| `disclosed_quantity` | `int`   | The disclosed quantity for the order.       | `0`        |
| `after_market_order` | `bool`  | Flag for after market order.                | `False`    |
| `validity`           | `str`   | The validity of the order (DAY, IOC, etc.). | `'DAY'`    |
| `amo_time`           | `str`   | The time for AMO orders.                    | `'OPEN'`   |
| `bo_profit_value`    | `float` | The profit value for BO orders.             | `None`     |
| `bo_stop_loss_Value` | `float` | The stop loss value for BO orders.          | `None`     |
| `tag`                | `str`   | Optional correlation ID for tracking.       | `None`     |
| `should_slice`       | `bool`  | (v2.2) route via slicing.                   | `False`    |

Returns: `dict` — response containing the status of the order placement.

### `place_slice_order(...)`

```python
place_slice_order(security_id, exchange_segment, transaction_type, quantity, order_type,
                  product_type, price, trigger_price=0, disclosed_quantity=0,
                  after_market_order=False, validity='DAY', amo_time='OPEN',
                  bo_profit_value=None, bo_stop_loss_Value=None, tag=None)
```

Place a new slice order in the Dhan account. Same parameters as `place_order` (without `should_slice`). Returns: `dict` — status of the slice order placement.

### `modify_order(order_id, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)`

Modify a pending order in the orderbook.

| Name                 | Type    | Description                              | Default    |
| -------------------- | ------- | ---------------------------------------- | ---------- |
| `order_id`           | `str`   | The ID of the order to modify.           | *required* |
| `order_type`         | `str`   | The type of order (e.g., LIMIT, MARKET). | *required* |
| `leg_name`           | `str`   | The name of the leg to modify.           | *required* |
| `quantity`           | `int`   | The new quantity for the order.          | *required* |
| `price`              | `float` | The new price for the order.             | *required* |
| `trigger_price`      | `float` | The trigger price for the order.         | *required* |
| `disclosed_quantity` | `int`   | The disclosed quantity for the order.    | *required* |
| `validity`           | `str`   | The validity of the order.               | *required* |

Returns: `dict` — status of the modification.

### `cancel_order(order_id)`

Cancel a pending order in the orderbook using the order ID. Returns: `dict` — status of the cancellation.

### `get_order_list()`

Retrieve a list of all orders requested in a day with their last updated status. Returns: `dict`.

### `get_order_by_id(order_id)`

Retrieve the details and status of an order from the orderbook placed during the day. Returns: `dict`.

### `get_order_by_correlationID(correlation_id)`

Retrieve the order status using a field called correlation_id (provided during order placement). Returns: `dict`.

---

## `SuperOrder` — `dhanhq._super_order`

```python
SuperOrder(dhan_context)
```

Interface to manage Dhan 'Super Orders', which are composite trading orders that include multiple legs: ENTRY_LEG, TARGET_LEG, and STOP_LOSS_LEG. These allow users to define entry, target, and stop-loss instructions as a bundled strategy.

### `place_super_order(security_id, exchange_segment, transaction_type, quantity, order_type, product_type, price, targetPrice, stopLossPrice, trailingJump, tag)`

Place a new Super Order on Dhan platform with entry, target and stop-loss legs.

| Name               | Type    | Description                                | Default    |
| ------------------ | ------- | ------------------------------------------ | ---------- |
| `security_id`      | `str`   | Instrument/security ID (required).         | *required* |
| `exchange_segment` | `str`   | Exchange (e.g., NSE, BSE).                 | *required* |
| `transaction_type` | `str`   | BUY or SELL.                               | *required* |
| `quantity`         | `int`   | Order quantity (> 0).                      | *required* |
| `order_type`       | `str`   | LIMIT, MARKET, etc.                        | *required* |
| `product_type`     | `str`   | CNC, INTRA, etc.                           | *required* |
| `price`            | `float` | Entry price.                               | *required* |
| `targetPrice`      | `float` | Target price.                              | *required* |
| `stopLossPrice`    | `float` | Stop loss price.                           | *required* |
| `trailingJump`     | `float` | Trailing SL value.                         | *required* |
| `tag`              | `str`   | Optional correlation ID or tracking label. | *required* |

Returns: `dict`. Raises: `ValueError` if mandatory inputs are missing or logically invalid.

### `modify_super_order(order_id, order_type, leg_name, quantity=0, price=0.0, targetPrice=0.0, stopLossPrice=0.0, trailingJump=0.0)`

Modify a pending super order leg. Based on the `leg_name` provided, modifies only the respective leg parameters.

| Name            | Type    | Description                                                | Default    |
| --------------- | ------- | ---------------------------------------------------------- | ---------- |
| `order_id`      | `str`   | ID of the order to modify (required).                      | *required* |
| `order_type`    | `str`   | Order type (e.g., LIMIT, MARKET).                          | *required* |
| `leg_name`      | `str`   | Must be one of ENTRY_LEG, TARGET_LEG, STOP_LOSS_LEG.       | *required* |
| `quantity`      | `int`   | Updated quantity (for ENTRY_LEG).                          | `0`        |
| `price`         | `float` | Updated price (for ENTRY_LEG).                             | `0.0`      |
| `targetPrice`   | `float` | Target price (for ENTRY_LEG or TARGET_LEG).                | `0.0`      |
| `stopLossPrice` | `float` | Stop loss price (for ENTRY_LEG or STOP_LOSS_LEG).          | `0.0`      |
| `trailingJump`  | `float` | Trailing jump trigger (for ENTRY_LEG or STOP_LOSS_LEG).    | `0.0`      |

Returns: `dict`. Raises: `ValueError` if leg_name or order_id is missing or invalid.

### `cancel_super_order(order_id, order_leg)`

Cancel a super order or a specific leg. Cancelling the main order ID cancels all legs. If a target or stop-loss leg is cancelled individually, it cannot be added again later.

| Name        | Type  | Description                                                  | Default    |
| ----------- | ----- | ------------------------------------------------------------ | ---------- |
| `order_id`  | `str` | The ID of the order to cancel (required).                    | *required* |
| `order_leg` | `str` | The leg to cancel: ENTRY_LEG, TARGET_LEG, or STOP_LOSS_LEG.  | *required* |

Returns: `dict`. Raises: `ValueError` if parameters are missing or invalid.

### `get_super_order_list()`

Retrieve a list of all live/requested super orders with their latest status. Returns: `dict`.

---

## `ForeverOrder` — `dhanhq._forever_order`

```python
ForeverOrder(dhan_context)
```

### `place_forever(...)`

```python
place_forever(security_id, exchange_segment, transaction_type, product_type, order_type,
              quantity, price, trigger_Price, order_flag='SINGLE', disclosed_quantity=0,
              validity='DAY', price1=0, trigger_Price1=0, quantity1=0, tag=None, symbol='')
```

Place a new forever order in the Dhan account.

| Name                 | Type    | Description                                 | Default    |
| -------------------- | ------- | ------------------------------------------- | ---------- |
| `security_id`        | `str`   | The ID of the security to trade.            | *required* |
| `exchange_segment`   | `str`   | The exchange segment (e.g., NSE, BSE).      | *required* |
| `transaction_type`   | `str`   | The type of transaction (BUY/SELL).         | *required* |
| `product_type`       | `str`   | The product type (e.g., CNC, INTRA).        | *required* |
| `order_type`         | `str`   | The type of order (LIMIT, MARKET, etc.).    | *required* |
| `quantity`           | `int`   | The quantity of the order.                  | *required* |
| `price`              | `float` | The price of the order.                     | *required* |
| `trigger_Price`      | `float` | The trigger price for the order.            | *required* |
| `order_flag`         | `str`   | The order flag (default is "SINGLE").       | `'SINGLE'` |
| `disclosed_quantity` | `int`   | The disclosed quantity for the order.       | `0`        |
| `validity`           | `str`   | The validity of the order (DAY, IOC, etc.). | `'DAY'`    |
| `price1`             | `float` | The secondary price for the order (OCO).    | `0`        |
| `trigger_Price1`     | `float` | The secondary trigger price (OCO).          | `0`        |
| `quantity1`          | `int`   | The secondary quantity (OCO).               | `0`        |
| `tag`                | `str`   | Optional correlation ID for tracking.       | `None`     |
| `symbol`             | `str`   | The trading symbol for the order.           | `''`       |

Returns: `dict`.

### `modify_forever(order_id, order_flag, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)`

Modify a forever order based on the specified leg name. The variables that can be modified include price, quantity, order type, and validity.

| Name                 | Type    | Description                                                      | Default    |
| -------------------- | ------- | ---------------------------------------------------------------- | ---------- |
| `order_id`           | `str`   | The ID of the order to modify.                                   | *required* |
| `order_flag`         | `str`   | The order flag indicating the type of order (e.g., SINGLE, OCO). | *required* |
| `order_type`         | `str`   | The type of order (e.g., LIMIT, MARKET).                         | *required* |
| `leg_name`           | `str`   | The name of the leg to modify.                                   | *required* |
| `quantity`           | `int`   | The new quantity for the order.                                  | *required* |
| `price`              | `float` | The new price for the order.                                     | *required* |
| `trigger_price`      | `float` | The trigger price for the order.                                 | *required* |
| `disclosed_quantity` | `int`   | The disclosed quantity for the order.                            | *required* |
| `validity`           | `str`   | The validity of the order.                                       | *required* |

Returns: `dict`.

### `cancel_forever(order_id)`

Delete Forever orders using the order id of an order. Returns: `dict`.

### `get_forever()`

Retrieve a list of all existing Forever Orders. Returns: `dict`.

---

## `Portfolio` — `dhanhq._portfolio`

```python
Portfolio(dhan_context)
```

### `get_holdings()`

Retrieve all holdings bought/sold in previous trading sessions. Returns: `dict`.

### `get_positions()`

Retrieve a list of all open positions for the day. Returns: `dict`.

### `convert_position(from_product_type, exchange_segment, position_type, security_id, convert_qty, to_product_type)`

Convert Position from Intraday to Delivery or vice versa.

| Name                | Type  | Description                                   | Default    |
| ------------------- | ----- | --------------------------------------------- | ---------- |
| `from_product_type` | `str` | The product type to convert from (e.g., CNC). | *required* |
| `exchange_segment`  | `str` | The exchange segment (e.g., NSE_EQ).          | *required* |
| `position_type`     | `str` | The type of position (e.g., LONG).            | *required* |
| `security_id`       | `str` | The ID of the security to convert.            | *required* |
| `convert_qty`       | `int` | The quantity to convert.                      | *required* |
| `to_product_type`   | `str` | The product type to convert to (e.g., CNC).   | *required* |

Returns: `dict`.

---

## `Funds` — `dhanhq._funds`

```python
Funds(dhan_context)
```

### `get_fund_limits()`

Get all information of your trading account like balance, margin utilized, collateral, etc. Returns: `dict`.

### `margin_calculator(security_id, exchange_segment, transaction_type, quantity, product_type, price, trigger_price=0)`

Calculate the margin required for a trade based on the provided parameters.

| Name               | Type    | Description                                                           | Default    |
| ------------------ | ------- | --------------------------------------------------------------------- | ---------- |
| `security_id`      | `str`   | The ID of the security for which the margin is to be calculated.      | *required* |
| `exchange_segment` | `str`   | The exchange segment (e.g., NSE_EQ) where the trade will be executed. | *required* |
| `transaction_type` | `str`   | The type of transaction (BUY/SELL).                                   | *required* |
| `quantity`         | `int`   | The quantity of the security to be traded.                            | *required* |
| `product_type`     | `str`   | The product type (e.g., CNC, INTRA) of the trade.                     | *required* |
| `price`            | `float` | The price at which the trade will be executed.                        | *required* |
| `trigger_price`    | `float` | The trigger price for the trade. Defaults to 0.                       | `0`        |

Returns: `dict` — margin calculation result.

---

## `Statement` — `dhanhq._statement`

```python
Statement(dhan_context)
```

### `get_trade_book(order_id=None)`

Retrieve a list of all trades executed in a day. If `order_id` is passed, retrieves trades of that specific order. Returns: `dict`.

### `get_trade_history(from_date, to_date, page_number=0)`

Retrieve the trade history for a specific date range (paginated). Returns: `dict`.

### `ledger_report(from_date, to_date)`

Retrieve the ledger details for a specific date range. Returns: `dict`.

---

## `TraderControl` — `dhanhq._trader_control`

```python
TraderControl(dhan_context)
```

### `kill_switch(action)`

Control kill switch for user, which will disable trading for current trading day.

| Name     | Type  | Description                                            | Default    |
| -------- | ----- | ------------------------------------------------------ | ---------- |
| `action` | `str` | 'activate' or 'deactivate' to control the kill switch. | *required* |

Returns: `dict` — Status of Kill Switch for account.

---

## `Security` — `dhanhq._security` (EDIS + Instrument list)

```python
Security(dhan_context)
```

Class attribute: `OTP_SENT = 'OTP sent'` (HTTP response constant).

### `fetch_security_list(mode='compact', filename='security_id_list.csv')` (staticmethod)

Fetch CSV file from dhan based on the specified mode and save it to the current directory.

| Name       | Type  | Description                                          | Default                  |
| ---------- | ----- | ---------------------------------------------------- | ------------------------ |
| `mode`     | `str` | The mode to fetch the CSV ('compact' or 'detailed'). | `'compact'`              |
| `filename` | `str` | The name of the file to save the CSV as.             | `'security_id_list.csv'` |

Returns: `pd.DataFrame` — DataFrame containing the CSV data.

### `generate_tpin()`

Generate T-Pin on registered mobile number. Returns: `dict`.

### `open_browser_for_tpin(isin, qty, exchange, segment='EQ', bulk=False)`

Opens the default web browser to enter T-Pin.

| Name       | Type   | Description                                    | Default    |
| ---------- | ------ | ---------------------------------------------- | ---------- |
| `isin`     | `str`  | The ISIN of the security.                      | *required* |
| `qty`      | `int`  | The quantity of the security.                  | *required* |
| `exchange` | `str`  | The exchange where the security is listed.     | *required* |
| `segment`  | `str`  | The segment of the exchange (default is 'EQ'). | `'EQ'`     |
| `bulk`     | `bool` | Flag for bulk operations (default is False).   | `False`    |

Returns: `dict`.

### `edis_inquiry(isin)`

Inquire about the eDIS status of the provided ISIN. Returns: `dict`.

---

## `HistoricalData` — `dhanhq._historical_data`

```python
HistoricalData(dhan_context)
```

### `historical_daily_data(security_id, exchange_segment, instrument_type, from_date, to_date, expiry_code=0)`

Retrieve OHLC & Volume of daily candle for desired instrument.

| Name               | Type  | Description                                   | Default    |
| ------------------ | ----- | --------------------------------------------- | ---------- |
| `security_id`      | `str` | Security ID of the instrument.                | *required* |
| `exchange_segment` | `str` | The exchange segment (e.g., NSE, BSE).        | *required* |
| `instrument_type`  | `str` | The type of instrument (e.g., stock, option). | *required* |
| `expiry_code`      | `str` | The expiry code for derivatives.              | `0`        |
| `from_date`        | `str` | The start date for the historical data.       | *required* |
| `to_date`          | `str` | The end date for the historical data.         | *required* |

Returns: `dict`.

### `intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval=1)`

Retrieve OHLC & Volume of minute candles for desired instrument for last 5 trading day (extended to 5 years per v2.2 API). `interval` accepts 1/5/15/25/60. Returns: `dict`.

### `expired_options_data(...)` (v2.2.0)

```python
expired_options_data(security_id, exchange_segment, instrument_type, expiry_flag,
                     expiry_code, strike, drv_option_type, required_data,
                     from_date, to_date)
```

Fetch expired options contract data on a rolling basis (see [Expired Options Data REST API](./19-expired-options-data.md)). Example in Quickstart above.

---

## `MarketFeed` (REST Market Quote mixin) — `dhanhq._market_feed`

```python
MarketFeed(dhan_context)
```

### `ticker_data(securities)`

Retrieve the latest market price (LTP) for specified instruments.

| Name         | Type   | Description                                                                                                             | Default    |
| ------------ | ------ | ------------------------------------------------------------------------------------------------------------------------ | ---------- |
| `securities` | `dict` | A dictionary where keys are exchange segments and values are lists of security IDs, e.g. `{"NSE_EQ": [11536], "NSE_FNO": [49081, 49082]}` | *required* |

Returns: `dict` — last traded price (LTP) data.

### `ohlc_data(securities)`

Retrieve the Open, High, Low and Close price along with LTP for specified instruments. Same `securities` dict format. Returns: `dict`.

### `quote_data(securities)`

Retrieve full details including market depth, OHLC data, OI and volume along with LTP for specified instruments. Same `securities` dict format. Returns: `dict` — full packet including market depth, last trade, circuit limit, OHLC, OI and volume data.

---

## `OptionChain` — `dhanhq._option_chain`

```python
OptionChain(dhan_context)
```

### `option_chain(under_security_id, under_exchange_segment, expiry)`

Retrieve the real-time Option Chain for a specified underlying instrument.

| Name                     | Type  | Description                                                         | Default    |
| ------------------------ | ----- | ------------------------------------------------------------------- | ---------- |
| `under_security_id`      | `int` | The security ID of the underlying instrument.                       | *required* |
| `under_exchange_segment` | `str` | The exchange segment of the underlying instrument (e.g., NSE, BSE). | *required* |
| `expiry`                 | `str` | The expiry date of the options.                                     | *required* |

Returns: `dict` — OI, Greeks, Volume, LTP, Best Bid/Ask, and IV across all strikes.

### `expiry_list(under_security_id, under_exchange_segment)`

Retrieve the dates of all expiries for a specified underlying instrument. Returns: `dict` — list of expiry dates.

---
Copyright © Dhan / Moneylicious Securities Private Limited. SDK licensed under MIT.

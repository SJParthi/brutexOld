# DhanHQ Python SDK — WebSocket Helper Classes (MarketFeed, OrderUpdate, FullDepth)

> Sources:
> - https://dhanhq.co/docs/DhanHQ-py/marketfeed/ (Live Market Feed class reference)
> - https://dhanhq.co/docs/DhanHQ-py/orderupdate/ (Live Order Update class reference)
> - https://dhanhq.co/docs/DhanHQ-py/fulldepth/ (Full Market Depth class reference)
> - https://raw.githubusercontent.com/dhan-oss/DhanHQ-py/main/README.md (usage examples)
> Captured: 2026-07-15
>
> REST client is documented in [23-python-sdk.md](./23-python-sdk.md). Underlying wire protocol: [15-live-market-feed.md](./15-live-market-feed.md), [16-full-market-depth.md](./16-full-market-depth.md), [13-live-order-update.md](./13-live-order-update.md).

## MarketFeed — `dhanhq.marketfeed.MarketFeed`

The marketfeed class is designed to facilitate asynchronous communication with the DhanHQ API via WebSocket. It enables users to subscribe to market data for a list of instruments and receive real-time updates.

```python
MarketFeed(dhan_context, instruments, version='v1')
```

**Class constants**
- Exchange segment constants (numeric, per [Annexure](./18-annexure.md#exchange-segment)): `MarketFeed.IDX` = 0, `MarketFeed.NSE` = 1, `MarketFeed.NSE_FNO` = 2, `MarketFeed.NSE_CURRENCY` = 3, `MarketFeed.BSE` = 4, `MarketFeed.MCX` = 5, `MarketFeed.BSE_CURRENCY` = 7, `MarketFeed.BSE_FNO = 8`
- Request code constants: `MarketFeed.Ticker` (15), `MarketFeed.Quote` (17), `MarketFeed.Full` (21) — unsubscribe codes 16/18/22 respectively
- `market_feed_wss = 'wss://api-feed.dhan.co'` — WebSocket URL for DhanHQ Live Market Feed

**Methods**

| Method | Async | Description |
| ------ | ----- | ----------- |
| `run_forever()` | no | Starts the WebSocket connection and runs the event loop. |
| `get_data()` | no | Fetch instruments data while the event loop is open. |
| `close_connection()` | no | Close WebSocket connection. |
| `connect()` | async | Initiates the connection to the Websockets. |
| `authorize()` | async | Establishes the WebSocket connection and authorizes the user. |
| `disconnect()` | async | Closes the WebSocket connection and sends a disconnect message. |
| `on_connection_opened(websocket)` | async | Callback executed when the WebSocket connection is opened. |
| `subscribe_instruments()` | async | Subscribe Instruments on the open Websocket. |
| `subscribe_symbols(symbols)` | no | Subscribe to additional symbols when connection is already established. |
| `unsubscribe_symbols(symbols)` | no | Unsubscribe symbols from an active connection. |
| `get_instrument_data()` | async | Fetches data and initiates process for conversion. |
| `create_header(feed_request_code, message_length, client_id)` | no | Creates header packet for the subscription packet. |
| `create_subscription_packet(instruments, feed_request_code)` | no | Creates the subscription packet with specified instruments and subscription code. |
| `pad_with_zeros(data, length)` | no | Pads a binary data string with zeros to a specified length for server to read. |
| `process_data(data)` | no | Read binary data and initiate processing in received format. |
| `process_ticker(data)` | no | Parse and process Ticker Data. |
| `process_quote(data)` | no | Parse and process Quote Data. |
| `process_full(data)` | no | Parse and process Full Packet Data. |
| `process_market_depth(data)` | no | Parse and process Market Depth Data. |
| `process_oi(data)` | no | Parse and process OI Data. |
| `process_prev_close(data)` | no | Parse and process Previous Day Data. |
| `process_status(data)` | no | Parse and process market status. |
| `server_disconnection(data)` | no | Parse and process server disconnection error. |
| `get_exchange_segment(exchange_code)` | no | Convert numeric exchange code to string representation. |
| `utc_time(epoch_time)` | no | Converts EPOCH time to UTC time. |
| `validate_and_process_tuples(tuples_list, batch_size=100)` | no | Create a list of all instruments to be added and add in batches of 100. |

### Usage (from official README)

```python
from dhanhq import DhanContext, MarketFeed

# Define and use your dhan_context if you haven't already done so like below:
dhan_context = DhanContext("client_id","access_token")

# Structure for subscribing is (exchange_segment, "security_id", subscription_type)

instruments = [(MarketFeed.NSE, "1333", MarketFeed.Ticker),   # Ticker - Ticker Data
    (MarketFeed.NSE, "1333", MarketFeed.Quote),     # Quote - Quote Data
    (MarketFeed.NSE, "1333", MarketFeed.Full),      # Full - Full Packet
    (MarketFeed.NSE, "11915", MarketFeed.Ticker),
    (MarketFeed.NSE, "11915", MarketFeed.Full)]

version = "v2"          # Mention Version and set to latest version 'v2'

# In case subscription_type is left as blank, by default Ticker mode will be subscribed.

try:
    data = MarketFeed(dhan_context, instruments, version)
    data.run_forever()

    while True:
        response = data.get_data()
        print(response)

except Exception as e:
    print(e)
```

```python
# Close Connection
data.close_connection()

# Subscribe instruments while connection is open
sub_instruments = [(MarketFeed.NSE, "14436", MarketFeed.Ticker)]

data.subscribe_symbols(sub_instruments)

# Unsubscribe instruments which are already active on connection
unsub_instruments = [(MarketFeed.NSE, "1333", 16)]

data.unsubscribe_symbols(unsub_instruments)
```

## OrderUpdate — `dhanhq.orderupdate.OrderUpdate`

The orderupdate class is designed to facilitate asynchronous communication with the DhanHQ API via WebSocket. It enables users to subscribe and receive real-time order updates.

```python
OrderUpdate(dhan_context)
```

**Attributes**

| Name             | Type  | Description                          |
| ---------------- | ----- | ------------------------------------ |
| `client_id`      | `str` | The client ID for authentication.    |
| `access_token`   | `str` | The access token for authentication. |
| `order_feed_wss` | `str` | The WebSocket URL for order updates (`wss://api-order-update.dhan.co`). |

**Methods**

| Method | Async | Description |
| ------ | ----- | ----------- |
| `connect_order_update()` | async | Connects to the WebSocket and listens for order updates. Authenticates the client and processes incoming messages. |
| `connect_to_dhan_websocket_sync()` | no | Synchronously connects to the WebSocket. Runs the asynchronous `connect_order_update` in a new event loop. |
| `handle_order_update(order_update)` | no | Handles incoming order update messages (`order_update: dict` — message received from the WebSocket). |

An optional `on_update` callback attribute can be attached to receive and process order data (see example).

### Usage (from official README)

```python
from dhanhq import DhanContext, OrderUpdate
import time

# Define and use your dhan_context if you haven't already done so like below:
dhan_context = DhanContext("client_id","access_token")

def on_order_update(order_data: dict):
    """Optional callback function to process order data"""
    print(order_data["Data"])

def run_order_update():
    order_client = OrderUpdate(dhan_context)

    # Optional: Attach a callback function to receive and process order data.
    order_client.on_update = on_order_update

    while True:
        try:
            order_client.connect_to_dhan_websocket_sync()
        except Exception as e:
            print(f"Error connecting to Dhan WebSocket: {e}. Reconnecting in 5 seconds...")
            time.sleep(5)

run_order_update()
```

## FullDepth — `dhanhq.fulldepth.FullDepth`

The FullDepth class is designed to facilitate asynchronous communication with the DhanHQ API via WebSocket. It enables users to subscribe to 20/200-level market depth for a list of instruments and receive real-time updates.

```python
FullDepth(dhan_context, instruments)          # optionally FullDepth(dhan_context, instruments, depth_level)
```

**Class constants**
- Exchange segment constants (numeric): e.g. `FullDepth.NSE_FNO = 2` (NSE = 1, etc., per [Annexure](./18-annexure.md#exchange-segment))
- `depth_feed_wss = 'wss://depth-api-feed.dhan.co/twentydepth'` — WebSocket URL for 20-level depth (200-level uses `wss://full-depth-api.dhan.co/twohundreddepth`)

**Methods**

| Method | Async | Description |
| ------ | ----- | ----------- |
| `run_forever()` | no | Starts the WebSocket connection and runs the event loop. |
| `get_data()` | no | Fetch instruments data while the event loop is open. |
| `close_connection()` | no | Close WebSocket connection. |
| `connect()` | async | Initiates the connection to the Websockets. |
| `disconnect()` | async | Closes the WebSocket connection and sends a disconnect message. |
| `on_connection_opened(websocket)` | async | Callback executed when the WebSocket connection is opened. |
| `subscribe_instruments()` | async | Subscribe Instruments on the open Websocket. |
| `subscribe_symbols(symbols)` | no | Subscribe to additional symbols when connection is already established. |
| `unsubscribe_symbols(symbols)` | no | Unsubscribe symbols from an active connection. |
| `get_instrument_data()` | async | Fetches data and processes messages one at a time. |
| `create_header(feed_request_code, message_length, client_id)` | no | Creates header packet for the subscription packet. |
| `create_subscription_packet(instruments, feed_request_code)` | no | Creates the subscription packet with specified instruments and subscription code. |
| `pad_with_zeros(data, length)` | no | Pads a binary data string with zeros to a specified length for server to read. |
| `process_data(data)` | no | Read binary data and process messages one at a time. |
| `process_depth_data(data, is_bid=True)` | no | Parse and process depth data for both bid and ask. |
| `combine_and_format_depth(bid_data, ask_data)` | no | Combine and sort bid/ask data, then format for display. |
| `server_disconnection(data)` | no | Parse and process server disconnection error. |
| `get_exchange_segment(exchange_code)` | no | Convert numeric exchange code to string representation. |
| `utc_time(epoch_time)` | no | Converts EPOCH time to UTC time. |
| `validate_and_process_tuples(tuples_list, batch_size=50)` | no | Create a list of all instruments to be added; maximum of 50 instruments in single socket. |

### Usage (from official README)

```python
from dhanhq import DhanContext, FullDepth

dhan_context = DhanContext(client_id, access_token)

instruments = [(1, "1333")]                     #[(1, "1333"),(2,"")] for 20 depth, upto 50 instruments
depth_level = 200                               # 20 or 200, default 20 in case this is not passed

try:
    response = FullDepth(dhan_context, instruments, depth_level)          #depth_level is non mandatory for 20 depth
    response.run_forever()

    while True:
        response.get_data()

        if response.on_close:
            print("Server disconnection detected. Kindly try again.")
            break

except Exception as e:
    print(e)
```

---
Copyright © Dhan / Moneylicious Securities Private Limited. SDK licensed under MIT.

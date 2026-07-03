# Groww Trade API — Annexures (All Enums & Constants)

> **Source:** https://groww.in/trade-api/docs/python-sdk/annexures
> **Captured:** 2026-07-03 (direct page capture, verified lossless: all headings, tables, and code blocks match the live page 1:1)

---

# Annexures

The SDK uses several fixed parameters to represent various trading parameters.

## [Order Status](#order-status)

| Value | Description |
| --- | --- |
| NEW | Order is newly created and pending for further processing |
| ACKED | Order has been acknowledged by the system |
| TRIGGER\_PENDING | Order is waiting for a trigger event to be executed |
| APPROVED | Order has been approved and is ready for execution |
| REJECTED | Order has been rejected by the system |
| FAILED | Order execution has failed |
| EXECUTED | Order has been successfully executed |
| DELIVERY\_AWAITED | Order has been executed and waiting for delivery |
| CANCELLED | Order has been cancelled |
| CANCELLATION\_REQUESTED | Request to cancel the order has been initiated |
| MODIFICATION\_REQUESTED | Request to modify the order has been initiated |
| COMPLETED | Order has been completed |

## [After Market Order Status](#after-market-order-status)

| Value | Description |
| --- | --- |
| NA | Status not available |
| PENDING | Order is pending for execution |
| DISPATCHED | Order has been dispatched for execution |
| PARKED | Order is parked for later execution |
| PLACED | Order has been placed in the market |
| FAILED | Order execution has failed |
| MARKET | Order is a market order |

## [Exchange](#exchange)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.EXCHANGE_BSE` | BSE | Bombay Stock Exchange - Asia's oldest exchange, known for SENSEX index |
| `GrowwAPI.EXCHANGE_NSE` | NSE | National Stock Exchange - India's largest exchange by trading volume |
| `GrowwAPI.EXCHANGE_MCX` | MCX | Multi Commodity Exchange - India's largest commodity derivatives exchange |

## [Segment](#segment)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.SEGMENT_CASH` | CASH | Regular equity market for trading stocks with delivery option |
| `GrowwAPI.SEGMENT_FNO` | FNO | Futures and Options segment for trading derivatives contracts |
| `GrowwAPI.SEGMENT_COMMODITY` | COMMODITY | Commodity derivatives segment for trading commodity futures and options on MCX |

## [Order Type](#order-type)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.ORDER_TYPE_LIMIT` | LIMIT | Specify exact price, may not get filled immediately but ensures price control |
| `GrowwAPI.ORDER_TYPE_MARKET` | MARKET | Immediate execution at best available price, no price guarantee |
| `GrowwAPI.ORDER_TYPE_STOP_LOSS` | SL | Stop Loss - Protection order that triggers at specified price to limit losses |
| `GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET` | SL\_M | Stop Loss Market - Market order triggered at specified price to limit losses |

## [Product](#product)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.PRODUCT_CNC` | CNC | Cash and Carry - For delivery-based equity trading with full upfront payment |
| `GrowwAPI.PRODUCT_MIS` | MIS | Margin Intraday Square-off - Higher leverage but must close by day end |
| `GrowwAPI.PRODUCT_NRML` | NRML | Regular margin trading allowing overnight positions with standard leverage |

## [Transaction Type](#transaction-type)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.TRANSACTION_TYPE_BUY` | BUY | Long position - Profit from price increase, loss from price decrease |
| `GrowwAPI.TRANSACTION_TYPE_SELL` | SELL | Short position - Profit from price decrease, loss from price increase |

## [Validity](#validity)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.VALIDITY_DAY` | DAY | Valid until market close on the same trading day |

## [Candle Interval](#candle-interval)

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.CANDLE_INTERVAL_MIN_1` | 1minute | 1 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_2` | 2minute | 2 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_3` | 3minute | 3 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_5` | 5minute | 5 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_10` | 10minute | 10 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_15` | 15minute | 15 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_30` | 30minute | 30 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_1` | 1hour | 1 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_4` | 4hour | 4 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_DAY` | 1day | 1 day interval |
| `GrowwAPI.CANDLE_INTERVAL_WEEK` | 1week | 1 week interval |
| `GrowwAPI.CANDLE_INTERVAL_MONTH` | 1month | 1 month interval |

## [Instrument Type](#instrument-type)

| Value | Description |
| --- | --- |
| EQ | Equity - Represents ownership in a company |
| IDX | Index - Composite value of a group of stocks representing a market |
| FUT | Futures - Derivatives contract to buy/sell an asset at a future date |
| CE | Call Option - Derivatives contract giving the right to buy an asset |
| PE | Put Option - Derivatives contract giving the right to sell an asset |

[Previous

User](/trade-api/docs/python-sdk/user)[Next

Exceptions](/trade-api/docs/python-sdk/exceptions)

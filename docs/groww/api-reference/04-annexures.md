# 04 — Annexures (Constants & Enums) · Groww Trading API (Python SDK)

> **Source:** https://groww.in/trade-api/docs/python-sdk/annexures
> **Verified:** 12 June 2026

The SDK uses several fixed parameters to represent various trading parameters.

## Order Status

| Value | Description |
| --- | --- |
| `NEW` | Order is newly created and pending for further processing |
| `ACKED` | Order has been acknowledged by the system |
| `TRIGGER_PENDING` | Order is waiting for a trigger event to be executed |
| `APPROVED` | Order has been approved and is ready for execution |
| `REJECTED` | Order has been rejected by the system |
| `FAILED` | Order execution has failed |
| `EXECUTED` | Order has been successfully executed |
| `DELIVERY_AWAITED` | Order has been executed and waiting for delivery |
| `CANCELLED` | Order has been cancelled |
| `CANCELLATION_REQUESTED` | Request to cancel the order has been initiated |
| `MODIFICATION_REQUESTED` | Request to modify the order has been initiated |
| `COMPLETED` | Order has been completed |

## After Market Order Status

| Value | Description |
| --- | --- |
| `NA` | Status not available |
| `PENDING` | Order is pending for execution |
| `DISPATCHED` | Order has been dispatched for execution |
| `PARKED` | Order is parked for later execution |
| `PLACED` | Order has been placed in the market |
| `FAILED` | Order execution has failed |
| `MARKET` | Order is a market order |

## Exchange

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.EXCHANGE_BSE` | `BSE` | Bombay Stock Exchange — Asia's oldest exchange, known for SENSEX index |
| `GrowwAPI.EXCHANGE_NSE` | `NSE` | National Stock Exchange — India's largest exchange by trading volume |
| `GrowwAPI.EXCHANGE_MCX` | `MCX` | Multi Commodity Exchange — India's largest commodity derivatives exchange |

## Segment

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.SEGMENT_CASH` | `CASH` | Regular equity market for trading stocks with delivery option |
| `GrowwAPI.SEGMENT_FNO` | `FNO` | Futures and Options segment for trading derivatives contracts |
| `GrowwAPI.SEGMENT_COMMODITY` | `COMMODITY` | Commodity derivatives segment for trading commodity futures and options on MCX |

## Order Type

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.ORDER_TYPE_LIMIT` | `LIMIT` | Specify exact price; may not get filled immediately but ensures price control |
| `GrowwAPI.ORDER_TYPE_MARKET` | `MARKET` | Immediate execution at best available price, no price guarantee |
| `GrowwAPI.ORDER_TYPE_STOP_LOSS` | `SL` | Stop Loss — protection order that triggers at specified price to limit losses |
| `GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET` | `SL_M` | Stop Loss Market — market order triggered at specified price to limit losses |

## Product

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.PRODUCT_CNC` | `CNC` | Cash and Carry — for delivery-based equity trading with full upfront payment |
| `GrowwAPI.PRODUCT_MIS` | `MIS` | Margin Intraday Square-off — higher leverage but must close by day end |
| `GrowwAPI.PRODUCT_NRML` | `NRML` | Regular margin trading allowing overnight positions with standard leverage |

## Transaction Type

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.TRANSACTION_TYPE_BUY` | `BUY` | Long position — profit from price increase, loss from price decrease |
| `GrowwAPI.TRANSACTION_TYPE_SELL` | `SELL` | Short position — profit from price decrease, loss from price increase |

## Validity

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.VALIDITY_DAY` | `DAY` | Valid until market close on the same trading day |

## Candle Interval

| SDK Constant | Value | Description |
| --- | --- | --- |
| `GrowwAPI.CANDLE_INTERVAL_MIN_1` | `1minute` | 1 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_2` | `2minute` | 2 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_3` | `3minute` | 3 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_5` | `5minute` | 5 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_10` | `10minute` | 10 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_15` | `15minute` | 15 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_30` | `30minute` | 30 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_1` | `1hour` | 1 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_4` | `4hour` | 4 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_DAY` | `1day` | 1 day interval |
| `GrowwAPI.CANDLE_INTERVAL_WEEK` | `1week` | 1 week interval |
| `GrowwAPI.CANDLE_INTERVAL_MONTH` | `1month` | 1 month interval |

## Instrument Type

| Value | Description |
| --- | --- |
| `EQ` | Equity — represents ownership in a company |
| `IDX` | Index — composite value of a group of stocks representing a market |
| `FUT` | Futures — derivatives contract to buy/sell an asset at a future date |
| `CE` | Call Option — derivatives contract giving the right to **buy** an asset |
| `PE` | Put Option — derivatives contract giving the right to **sell** an asset |

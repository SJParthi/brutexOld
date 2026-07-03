# Groww Trade API — REST API: Annexures

> **Source:** https://groww.in/trade-api/docs/curl/annexures
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

| Value | Description |
| --- | --- |
| BSE | Bombay Stock Exchange - Asia's oldest exchange, known for SENSEX index |
| NSE | National Stock Exchange - India's largest exchange by trading volume |
| MCX | Multi Commodity Exchange - India's largest commodity derivatives exchange |

## [Segment](#segment)

| Value | Description |
| --- | --- |
| CASH | Regular equity market for trading stocks with delivery option |
| FNO | Futures and Options segment for trading derivatives contracts |
| COMMODITY | Commodity derivatives segment for trading commodity futures and options on MCX |

## [Order Type](#order-type)

| Value | Description |
| --- | --- |
| LIMIT | Specify exact price, may not get filled immediately but ensures price control |
| MARKET | Immediate execution at best available price, no price guarantee |
| SL | Stop Loss - Protection order that triggers at specified price to limit losses |
| SL\_M | Stop Loss Market - Market order triggered at specified price to limit losses |

## [Product](#product)

| Value | Description |
| --- | --- |
| CNC | Cash and Carry - For delivery-based equity trading with full upfront payment |
| MIS | Margin Intraday Square-off - Higher leverage but must close by day end |
| NRML | Regular margin trading allowing overnight positions with standard leverage |

## [Transaction Type](#transaction-type)

| Value | Description |
| --- | --- |
| BUY | Long position - Profit from price increase, loss from price decrease |
| SELL | Short position - Profit from price decrease, loss from price increase |

## [Validity](#validity)

| Value | Description |
| --- | --- |
| DAY | Valid until market close on the same trading day |

## [Candle Interval](#candle-interval)

| Value | Description |
| --- | --- |
| 1minute | 1 minute interval |
| 2minute | 2 minute interval |
| 3minute | 3 minute interval |
| 5minute | 5 minute interval |
| 10minute | 10 minute interval |
| 15minute | 15 minute interval |
| 30minute | 30 minute interval |
| 1hour | 1 hour interval |
| 4hour | 4 hour interval |
| 1day | 1 day interval |
| 1week | 1 week interval |
| 1month | 1 month interval |

## [Instrument Type](#instrument-type)

| Value | Description |
| --- | --- |
| EQ | Equity - Represents ownership in a company |
| IDX | Index - Composite value of a group of stocks representing a market |
| FUT | Futures - Derivatives contract to buy/sell an asset at a future date |
| CE | Call Option - Derivatives contract giving the right to buy an asset |
| PE | Put Option - Derivatives contract giving the right to sell an asset |

[Previous

User](/trade-api/docs/curl/user)[Next

Changelog](/trade-api/docs/curl/changelog)

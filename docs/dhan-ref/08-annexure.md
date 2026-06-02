# Annexure (enums) — DhanHQ v2

> Source: https://dhanhq.co/docs/v2/annexure/ · API v2 · Audited 2 Jun 2026

All enums, codes and error tables used across DhanHQ v2 APIs.

## Exchange Segment
String attribute (used in REST/WS JSON) **and** integer enum (used inside binary WS packets and Python `MarketFeed`/`FullDepth` constants).
| Attribute | Exchange | Segment | enum |
|---|---|---|---|
| `IDX_I` | Index | Index Value | `0` |
| `NSE_EQ` | NSE | Equity Cash | `1` |
| `NSE_FNO` | NSE | Futures & Options | `2` |
| `NSE_CURRENCY` | NSE | Currency | `3` |
| `BSE_EQ` | BSE | Equity Cash | `4` |
| `MCX_COMM` | MCX | Commodity | `5` |
| `BSE_CURRENCY` | BSE | Currency | `7` |
| `BSE_FNO` | BSE | Futures & Options | `8` |

## Product Type
| Attribute | Detail |
|---|---|
| `CNC` | Cash & Carry (equity delivery) |
| `INTRADAY` | Intraday (Equity, F&O) |
| `MARGIN` | Carry-forward in F&O |
| `CO` | Cover Order |
| `BO` | Bracket Order |

> `CO` & `BO` are valid for Intraday only.

## Order Status
| Attribute | Detail |
|---|---|
| `TRANSIT` | Did not reach the exchange server |
| `PENDING` | Awaiting execution |
| `CLOSED` | Super Order: both entry & exit placed |
| `TRIGGERED` | Super Order: Target or Stop-Loss leg triggered |
| `REJECTED` | Rejected by broker/exchange |
| `CANCELLED` | Cancelled by user |
| `PART_TRADED` | Partial quantity traded |
| `TRADED` | Executed successfully |

## After Market Order time
| Attribute | Detail |
|---|---|
| `PRE_OPEN` | Pumped at pre-market session |
| `OPEN` | Pumped at market open |
| `OPEN_30` | 30 min after open |
| `OPEN_60` | 60 min after open |

## Expiry Code
| Attribute | Detail |
|---|---|
| `0` | Current / near expiry |
| `1` | Next expiry |
| `2` | Far expiry |

## Instrument
| Attribute | Detail |
|---|---|
| `INDEX` | Index |
| `FUTIDX` | Futures of Index |
| `OPTIDX` | Options of Index |
| `EQUITY` | Equity |
| `FUTSTK` | Futures of Stock |
| `OPTSTK` | Options of Stock |
| `FUTCOM` | Futures of Commodity |
| `OPTFUT` | Options of Commodity Futures |
| `FUTCUR` | Futures of Currency |
| `OPTCUR` | Options of Currency |

## Feed Request Code (client → server)
| Attribute | Detail |
|---|---|
| `11` | Connect Feed |
| `12` | Disconnect Feed |
| `15` | Subscribe — Ticker Packet |
| `16` | Unsubscribe — Ticker Packet |
| `17` | Subscribe — Quote Packet |
| `18` | Unsubscribe — Quote Packet |
| `21` | Subscribe — Full Packet |
| `22` | Unsubscribe — Full Packet |
| `23` | Subscribe — Full Market Depth |
| `24` | Unsubscribe — Full Market Depth |

## Feed Response Code (server → client)
| Attribute | Detail |
|---|---|
| `1` | Index Packet |
| `2` | Ticker Packet |
| `4` | Quote Packet |
| `5` | OI Packet |
| `6` | Prev Close Packet |
| `7` | Market Status Packet |
| `8` | Full Packet |
| `50` | Feed Disconnect |

> Depth feed also uses `41` (Bid/Buy data) and `51` (Ask/Sell data) — see `04_full_market_depth.md`.

## Trading API Error (DH-9xx)
| Type | Code | Message |
|---|---|---|
| Invalid Authentication | `DH-901` | Client ID or access token invalid/expired |
| Invalid Access | `DH-902` | Not subscribed to Data APIs or no Trading-API access |
| User Account | `DH-903` | Account issue (segment not activated, etc.) |
| Rate Limit | `DH-904` | Too many requests; throttle |
| Input Exception | `DH-905` | Missing fields / bad parameter values |
| Order Error | `DH-906` | Incorrect order request |
| Data Error | `DH-907` | Cannot fetch data (bad params / no data) |
| Internal Server Error | `DH-908` | Rare server-side failure |
| Network Error | `DH-909` | API could not reach backend |
| Others | `DH-910` | Other reasons |

## Data API Error / WebSocket disconnect codes (8xx)
| Code | Description |
|---|---|
| `800` | Internal Server Error |
| `804` | Requested number of instruments exceeds limit |
| `805` | Too many requests/connections (user may be blocked; oldest WS dropped on 6th+ connection) |
| `806` | Data APIs not subscribed |
| `807` | Access token expired |
| `808` | Authentication failed — Client ID or Access Token invalid |
| `809` | Access token invalid |
| `810` | Client ID invalid |
| `811` | Invalid expiry date |
| `812` | Invalid date format |
| `813` | Invalid SecurityId |
| `814` | Invalid request |

## Conditional Triggers

### Comparison Type
| Type | Description | Mandatory fields |
|---|---|---|
| `TECHNICAL_WITH_VALUE` | Technical indicator vs fixed value | `indicatorName` `operator` `timeFrame` `comparingValue` |
| `TECHNICAL_WITH_INDICATOR` | Technical indicator vs another indicator | `indicatorName` `operator` `timeFrame` `comparingIndicatorName` |
| `TECHNICAL_WITH_CLOSE` | Technical indicator vs closing price | `indicatorName` `operator` `timeFrame` |
| `PRICE_WITH_VALUE` | Market price vs fixed value | `operator` `comparingValue` |

### Indicator Name
`SMA_5`, `SMA_10`, `SMA_20`, `SMA_50`, `SMA_100`, `SMA_200`, `EMA_5`, `EMA_10`, `EMA_20`, `EMA_50`, `EMA_100`, `EMA_200`, `BB_UPPER`, `BB_LOWER`, `RSI_14`, `ATR_14`, `STOCHASTIC`, `STOCHRSI_14`, `MACD_26` (long-term), `MACD_12` (short-term), `MACD_HIST` (histogram).

### Operator
`CROSSING_UP`, `CROSSING_DOWN`, `CROSSING_ANY_SIDE`, `GREATER_THAN`, `LESS_THAN`, `GREATER_THAN_EQUAL`, `LESS_THAN_EQUAL`, `EQUAL`, `NOT_EQUAL`.

### Status
| Status | Description |
|---|---|
| `ACTIVE` | Alert currently active |
| `TRIGGERED` | Alert condition met |
| `EXPIRED` | Alert expired |
| `CANCELLED` | Alert cancelled |

# Chart API — `historicalcharts` (Daily OHLC) — OpenAPI

> Source: https://api.dhan.co/v2/#/operations/historicalcharts (OpenAPI spec `https://api.dhan.co/v2/v3/api-docs`) · Audited 2 Jun 2026
> Machine-schema view of the daily endpoint described narratively in `05_historical_data.md`.

- **operationId:** `historicalcharts`
- **Method/Path:** `POST /charts/historical`
- **Summary:** fetch OHLC for daily candle
- **Description:** Retrieve OHLC & Volume of daily candle for desired instrument. Data for any scrip is available back to its inception date.

## Auth
| Param | In | Required | Type |
|---|---|---|---|
| `access-token` | header | ✓ | string |

## Request body (`application/json` → schema `HistoricalChartsRequest`)
| Field | Type | Enum / format | Notes |
|---|---|---|---|
| `securityId` | string | — | From instrument master |
| `exchangeSegment` | string | `NSE_EQ`,`NSE_FNO`,`BSE_EQ`,`BSE_FNO`,`MCX_COMM`,`IDX_I` | Exchange & segment |
| `instrument` | string | `INDEX`,`FUTIDX`,`OPTIDX`,`EQUITY`,`FUTSTK`,`OPTSTK`,`FUTCOM`,`OPTFUT` | Instrument type |
| `expiryCode` | integer | int32 | Derivatives expiry code |
| `oi` | boolean | — | Open Interest data |
| `fromDate` | string | date `YYYY-MM-DD` | Start |
| `toDate` | string | date `YYYY-MM-DD` | End (docs: non-inclusive) |

## Response 200 (`application/json` → schema `ChartsResponse`)
All fields are arrays of `double`, positionally aligned:
| Field | Element type | Description |
|---|---|---|
| `open` | double | Open per candle |
| `high` | double | High per candle |
| `low` | double | Low per candle |
| `close` | double | Close per candle |
| `volume` | double | Volume per candle |
| `timestamp` | double | Candle date (see note) |
| `open_interest` | double | OI per candle |

> **Timestamp:** OpenAPI text says "seconds since January 01, 1980" — conflicts with docs/release-notes + the Python library, which treat it as **Unix epoch (since 1970)**. Use Unix-1970 and convert to IST.
> `ChartsResponse` is shared with the intraday endpoint (`13_chart_intradaycharts.md`).

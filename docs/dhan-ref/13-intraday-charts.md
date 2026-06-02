# Chart API — `intradaycharts` (Intraday OHLC) — OpenAPI

> Source: https://api.dhan.co/v2/#/operations/intradaycharts (OpenAPI spec `https://api.dhan.co/v2/v3/api-docs`) · Audited 2 Jun 2026
> Machine-schema view of the intraday endpoint described narratively in `05_historical_data.md`.

- **operationId:** `intradaycharts`
- **Method/Path:** `POST /charts/intraday`
- **Summary:** fetch OHLC for minute timeframe
- **Description:** Retrieve OHLC & Volume of minute candle for desired instrument. Available for all segments including futures & options. (Docs-site note: up to ~5 years, max 90 days per call.)

## Auth
| Param | In | Required | Type |
|---|---|---|---|
| `access-token` | header | ✓ | string |

## Request body (`application/json` → schema `IntradayChartsRequest`)
| Field | Type | Enum / format | Notes |
|---|---|---|---|
| `securityId` | string | — | From instrument master |
| `exchangeSegment` | string | `NSE_EQ`,`NSE_FNO`,`BSE_EQ`,`BSE_FNO`,`MCX_COMM`,`IDX_I` | Exchange & segment |
| `instrument` | string | `INDEX`,`FUTIDX`,`OPTIDX`,`EQUITY`,`FUTSTK`,`OPTSTK`,`FUTCOM`,`OPTFUT` | Instrument type |
| `interval` | string | `1`,`5`,`15`,`25`,`60` | Minute interval |
| `oi` | boolean | — | Open Interest data |
| `fromDate` | string | `yyyy-MM-dd` (docs also allow `yyyy-MM-dd HH:MM:SS` IST) | Start |
| `toDate` | string | `yyyy-MM-dd` (or with time) | End |

## Response 200 (`application/json` → schema `ChartsResponse`)
All fields are arrays of `double`, positionally aligned:
| Field | Element type | Description |
|---|---|---|
| `open` | double | Open per candle |
| `high` | double | High per candle |
| `low` | double | Low per candle |
| `close` | double | Close per candle |
| `volume` | double | Volume per candle |
| `timestamp` | double | Candle time (see note) |
| `open_interest` | double | OI per candle |

> **Timestamp:** OpenAPI text says "seconds since January 01, 1980" — this conflicts with the docs/release-notes and the Python library, which treat it as **Unix epoch (since 1970)**. Use Unix-1970 and convert to IST.

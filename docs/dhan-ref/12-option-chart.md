# Chart API — `optionchart` (Expired/Rolling Options) — OpenAPI

> Source: https://api.dhan.co/v2/#/operations/optionchart (OpenAPI spec `https://api.dhan.co/v2/v3/api-docs`) · Audited 2 Jun 2026
> This is the machine-schema view of the same endpoint described narratively in `06_expired_options_data.md`.

- **operationId:** `optionchart`
- **Method/Path:** `POST /charts/rollingoption`
- **Summary:** fetch expired option data
- **Description:** Fetch minute-wise rolling option chart data of expired contracts — OHLC, volume, IV based on strike.

## Auth
| Param | In | Required | Type |
|---|---|---|---|
| `access-token` | header | ✓ | string |

## Request body (`application/json` → schema `OptionChartRequest`)
| Field | Type | Enum / format | Notes |
|---|---|---|---|
| `exchangeSegment` | string | `NSE_EQ`,`NSE_FNO`,`BSE_EQ`,`BSE_FNO`,`MCX_COMM`,`IDX_I` | Exchange & segment |
| `interval` | string | `1`,`5`,`15`,`25`,`60` | Minute interval |
| `securityId` | integer | int32 | **Underlying** security ID |
| `instrument` | string | `INDEX`,`FUTIDX`,`OPTIDX`,`EQUITY`,`FUTSTK`,`OPTSTK`,`FUTCOM`,`OPTFUT` | Instrument type |
| `expiryFlag` | string | `MONTH`,`WEEK` | Expiry type |
| `expiryCode` | integer | int32, enum `1`,`2`,`3` | Expiry code |
| `strike` | string | `ATM` (default); `ATM±3` (all instruments); `ATM±10` (index near expiry) | Strike relative to spot |
| `drvOptionType` | string | `CALL`,`PUT` | Option type |
| `requiredData` | array[string] | items ∈ `open`,`high`,`low`,`close`,`iv`,`volume`,`strike`,`oi`,`spot` | Which arrays to return |
| `fromDate` | string | date `YYYY-MM-DD` | Start date |
| `toDate` | string | date `YYYY-MM-DD` | End date |

## Response 200 (`application/json` → schema `OptionChartResponse`)
```
data: ChartData
  ce: OptionChartPayload | null
  pe: OptionChartPayload | null
```
**OptionChartPayload** (each field is an array; populated only if requested, else empty/null):
| Field | Element type |
|---|---|
| `iv` | double |
| `oi` | int64 |
| `strike` | double |
| `spot` | double |
| `open` | double |
| `high` | double |
| `low` | double |
| `close` | double |
| `volume` | int64 |
| `timestamp` | int64 (unix seconds; convert to IST) |

> The non-requested option side (`ce` or `pe`) is returned as `null`.

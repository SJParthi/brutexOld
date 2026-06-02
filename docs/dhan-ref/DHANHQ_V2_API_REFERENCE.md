# DhanHQ v2 — Complete API & Python SDK Reference

> **Purpose:** Single-file, precise context document for Claude Code to build a trading system on DhanHQ.
> **Audited:** 2 June 2026. **API version:** v2. **Python client:** `dhanhq` **2.2.0** (released 24 Apr 2026).
> **Provider:** Moneylicious Securities Pvt. Ltd. (Dhan).

This document is compiled from a full read of every page below. Field names, types, enum values, byte
offsets and method signatures are reproduced verbatim from source. Where the official human docs, the
machine OpenAPI spec, and the Python library disagree, the conflict is flagged in
**[§13 Gotchas](#13-gotchas--implementation-notes)** — read that section before coding.

**Sources audited**
- Docs site (MkDocs): Introduction, Authentication, Live Market Feed, Full Market Depth, Historical Data, Expired Options Data, Option Chain, Annexure, Instrument List, Releases, Live Order Update — all under `https://dhanhq.co/docs/v2/`
- OpenAPI 3.0.1 spec (Stoplight) backing `https://api.dhan.co/v2/` → served at `https://api.dhan.co/v2/v3/api-docs` (33 REST paths). Operations `optionchart`, `intradaycharts`, `historicalcharts` map to `/charts/rollingoption`, `/charts/intraday`, `/charts/historical`.
- `https://pypi.org/project/dhanhq/` (PyPI JSON metadata + README)
- `https://github.com/dhan-oss/DhanHQ-py` (source under `src/dhanhq/`, branch `main`)

---

## Table of contents
1. [Quick reference](#1-quick-reference)
2. [Rate limits](#2-rate-limits)
3. [Errors](#3-errors)
4. [Authentication](#4-authentication)
5. [Annexure — enums](#5-annexure--enums)
6. [Instrument master (scrip list)](#6-instrument-master-scrip-list)
7. [Historical Data API (REST)](#7-historical-data-api-rest)
8. [Expired Options Data API (REST)](#8-expired-options-data-api-rest)
9. [Option Chain API (REST)](#9-option-chain-api-rest)
10. [Live Market Feed (WebSocket, binary)](#10-live-market-feed-websocket-binary)
11. [Full Market Depth — 20 & 200 level (WebSocket, binary)](#11-full-market-depth--20--200-level-websocket-binary)
12. [Live Order Update (WebSocket, JSON)](#12-live-order-update-websocket-json)
13. [Gotchas & implementation notes](#13-gotchas--implementation-notes)
14. [Python SDK (`dhanhq`) reference](#14-python-sdk-dhanhq-reference)
15. [Appendix — full REST endpoint inventory](#15-appendix--full-rest-endpoint-inventory)

---

## 1. Quick reference

### Hosts / base URLs
| Purpose | URL |
|---|---|
| REST API base | `https://api.dhan.co/v2` |
| Auth (token, consent) base | `https://auth.dhan.co` |
| Direct token generation (TOTP) | `https://auth.dhan.co/app/generateAccessToken` |
| Live Market Feed (WS) | `wss://api-feed.dhan.co` |
| 20-level Market Depth (WS) | `wss://depth-api-feed.dhan.co/twentydepth` |
| 200-level Market Depth (WS) | `wss://full-depth-api.dhan.co/twohundreddepth` |
| Live Order Update (WS) | `wss://api-order-update.dhan.co` |
| Instrument master — compact CSV | `https://images.dhan.co/api-data/api-scrip-master.csv` |
| Instrument master — detailed CSV | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` |
| OpenAPI spec (machine) | `https://api.dhan.co/v2/v3/api-docs` |

### Request conventions (REST)
- GET / DELETE → parameters as **query params**. POST / PUT → parameters as **JSON body** (form-encoded historically; v2 uses JSON).
- Auth header on every request: `access-token: <JWT>`.
- Some Data APIs additionally require `client-id: <dhanClientId>` header (Option Chain explicitly does).
- Responses are JSON. Errors carry `errorType` / `errorCode` / `errorMessage`.

### Account requirements
- **Trading APIs are free** for all Dhan users (place/modify orders, positions, funds, etc.).
- **Data APIs are a paid add-on** (Historical, Live Feed, Full Depth, Option Chain, Market Quote). Check `dataPlan` / `dataValidity` via the Profile API. Missing subscription → error `DH-902` (REST) or `806` (WS).
- **Static IP whitelisting is mandatory** (SEBI/exchange rule) for **order-placement** APIs only (Orders, Super Order, Forever Order). Fetching order/trade details does **not** need it. See [§4](#static-ip-setup).
- **Access tokens last 24 hours** (since v2.4). API key & secret last **12 months**.

### Top gotchas (full list in §13)
- WS responses are **binary, little-endian**. The Live-Feed header and the Depth-feed header have **different field orders** — don't share a parser.
- **Timestamp epoch:** docs/release-notes + the Python lib say **Unix epoch (since 1970)**; the OpenAPI spec text wrongly says "since 1980". Treat as Unix epoch.
- Daily & expired-options `toDate` is **non-inclusive**. Intraday is documented as inclusive (date-time range).
- Intraday history: **max 90 days per call**. Expired options: **max 30 days per call**. Both go back **~5 years**.
- Option Chain rate limit: **1 unique request / 3 seconds**.

---

## 2. Rate limits

| Window | Order APIs | Data APIs | Quote APIs | Non-Trading APIs |
|---|---|---|---|---|
| per second | 10 | 5 | 1 | 20 |
| per minute | 250 | — | Unlimited | Unlimited |
| per hour | 1000 | — | Unlimited | Unlimited |
| per day | 7000 | 100000 | Unlimited | Unlimited |

- Order **modifications capped at 25 per order**.
- **Option Chain** has its own limit: **1 request per 3 seconds** (because OI updates slowly). You may fire concurrent unique requests for different underlyings/expiries within that 3-second cadence.
- Data APIs on minute/hour windows are effectively unlimited; the binding limits are **5/sec** and **100,000/day**.

---

## 3. Errors

REST error body shape:
```json
{ "errorType": "", "errorCode": "", "errorMessage": "" }
```

### Trading API errors (DH-9xx)
| Type | Code | Meaning |
|---|---|---|
| Invalid Authentication | `DH-901` | Client ID or access token invalid/expired. |
| Invalid Access | `DH-902` | Not subscribed to Data APIs, or no Trading-API access. |
| User Account | `DH-903` | Account-level issue (segment not activated, etc.). |
| Rate Limit | `DH-904` | Too many requests; throttle. |
| Input Exception | `DH-905` | Missing required fields / bad parameter values. |
| Order Error | `DH-906` | Order request cannot be processed. |
| Data Error | `DH-907` | Cannot fetch data (bad params / no data). |
| Internal Server Error | `DH-908` | Rare server-side failure. |
| Network Error | `DH-909` | API could not reach backend. |
| Others | `DH-910` | Other reasons. |

### Data API errors / WebSocket disconnect codes (8xx)
| Code | Meaning |
|---|---|
| `800` | Internal Server Error |
| `804` | Requested number of instruments exceeds limit |
| `805` | Too many requests/connections (further requests may get user blocked). Also: the **oldest** socket is dropped with `805` when a 6th+ WS connection opens. |
| `806` | Data APIs not subscribed |
| `807` | Access token expired |
| `808` | Authentication failed — client ID or access token invalid |
| `809` | Access token invalid |
| `810` | Client ID invalid |
| `811` | Invalid expiry date |
| `812` | Invalid date format |
| `813` | Invalid SecurityId |
| `814` | Invalid request |

---

## 4. Authentication

Two user categories: **Individual** (build your own algo on your own account) and **Partner** (platforms serving many users; requires `partner_id`/`partner_secret` from Dhan). All requests need a JWT `access-token` header.

### 4a. Individual — direct access token (manual)
Login to `web.dhan.co` → My Profile → **Access DhanHQ APIs** → generate **Access Token** (24-hour validity). Optionally set a Postback URL for order updates.

### 4b. Individual — generate token via API (requires TOTP enabled)
```
POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=1000000001&pin=11111&totp=000000
```
Query params: `dhanClientId`, `pin` (6-digit Dhan PIN), `totp` (6-digit from authenticator).

Response:
```json
{
  "dhanClientId": "1000000401",
  "dhanClientName": "JOHN DOE",
  "dhanClientUcc": "ABCD12345E",
  "givenPowerOfAttorney": false,
  "accessToken": "eyJ...",
  "expiryTime": "2026-01-01T00:00:00.000"
}
```
`givenPowerOfAttorney` = DDPI status. `expiryTime` = 24h from generation.

### 4c. Renew token (web-generated tokens only)
```
POST https://api.dhan.co/v2/RenewToken
Headers: access-token: {JWT}, dhanClientId: {Client ID}
```
Expires the current token and returns a fresh 24h token. **Only renews active tokens** — renewing an expired token errors. Works only for tokens generated from Dhan Web.

### 4d. Individual — API key & secret (OAuth, 3 steps)
Generate App name + Redirect URL + (optional) Postback URL at `web.dhan.co` → My Profile → Access DhanHQ APIs → **API key** toggle. **Key & secret valid 12 months.**

**Step 1 — Generate consent**
```
POST https://auth.dhan.co/app/generate-consent?client_id={dhanClientId}
Headers: app_id: {API key}, app_secret: {API secret}
```
→ returns `consentAppId` (status `GENERATED`). Up to **25 consentAppId/day**; each stays active until a `tokenId` is generated; only one token at a time.

**Step 2 — Browser login** (open in a browser; ends in a 302 redirect)
```
https://auth.dhan.co/login/consentApp-login?consentAppId={consentAppId}
```
→ redirects to `{redirect_URL}/?tokenId={tokenId}`. Consume `tokenId` from the path.

**Step 3 — Consume consent**
```
POST https://auth.dhan.co/app/consumeApp-consent?tokenId={tokenId}
Headers: app_id: {API key}, app_secret: {API secret}
```
Response (same shape as partner consume):
```json
{
  "dhanClientId": "1000000001",
  "dhanClientName": "JOHN DOE",
  "dhanClientUcc": "CEFE4265",
  "givenPowerOfAttorney": true,
  "accessToken": "<access token>",
  "expiryTime": "2025-09-23T12:37:23"
}
```

### 4e. Partner — 3-step login
1. `POST https://auth.dhan.co/partner/generate-consent` with headers `partner_id`, `partner_secret` → returns `consentId` (status `GENERATED`).
2. Open `https://auth.dhan.co/consent-login?consentId={consentId}` in browser/webview → 302 redirect with `tokenId`.
3. `POST https://auth.dhan.co/partner/consume-consent?tokenId={tokenId}` with headers `partner_id`, `partner_secret` → returns the access-token payload above.

### 4f. Static IP setup
Mandatory per SEBI/exchange rules for **order-placement** APIs. Supports IPv4 and IPv6. You may set a **PRIMARY** and a **SECONDARY** IP; each must be unique per user. **Once set, an IP cannot be modified for 7 days.** Can also be managed via `web.dhan.co`.

**Set** `POST https://api.dhan.co/v2/ip/setIP`
```json
{ "dhanClientId": "1000000001", "ip": "10.200.10.10", "ipFlag": "PRIMARY" }
```
`ipFlag` enum: `PRIMARY` | `SECONDARY`. Response: `{ "message": "IP saved successfully", "status": "SUCCESS" }`.

**Modify** `PUT https://api.dhan.co/v2/ip/modifyIP` — same body; only callable inside the allowed (every-7-days) window.

**Get** `GET https://api.dhan.co/v2/ip/getIP` (header `access-token` only)
```json
{
  "modifyDateSecondary": "2025-09-30",
  "secondaryIP": "10.420.43.12",
  "modifyDatePrimary": "2025-09-28",
  "primaryIP": "10.420.29.14"
}
```
`modifyDate*` = the date from which that IP becomes editable again (YYYY-MM-DD).

### 4g. TOTP setup
Time-based OTP (RFC 6238), 6 digits, refreshed every 30s. Enable at: Dhan Web → DhanHQ Trading APIs → **Setup TOTP** → confirm with mobile/email OTP → scan QR / enter secret into an authenticator → confirm first TOTP. Once enabled, TOTP becomes a login option everywhere and unlocks [§4b](#4b-individual--generate-token-via-api-requires-totp-enabled).

### 4h. User profile (token health check)
```
GET https://api.dhan.co/v2/profile
Headers: access-token: {JWT}
```
```json
{
  "dhanClientId": "1100003626",
  "tokenValidity": "30/03/2025 15:37",
  "activeSegment": "Equity, Derivative, Currency, Commodity",
  "ddpi": "Active",
  "mtf": "Active",
  "dataPlan": "Active",
  "dataValidity": "2024-12-05 09:37:52.0"
}
```
`ddpi`/`mtf`/`dataPlan` enum: `Active` | `Deactive`. Great first call to validate integration.

---

## 5. Annexure — enums

### Exchange Segment
**Two representations exist:** a string attribute (used in REST JSON bodies and WS JSON) and an integer enum (used inside binary WS packets and in the Python `MarketFeed`/`FullDepth` constants).
| Attribute (string) | Exchange | Segment | int enum |
|---|---|---|---|
| `IDX_I` | Index | Index Value | `0` |
| `NSE_EQ` | NSE | Equity Cash | `1` |
| `NSE_FNO` | NSE | Futures & Options | `2` |
| `NSE_CURRENCY` | NSE | Currency | `3` |
| `BSE_EQ` | BSE | Equity Cash | `4` |
| `MCX_COMM` | MCX | Commodity | `5` |
| `BSE_CURRENCY` | BSE | Currency | `7` |
| `BSE_FNO` | BSE | Futures & Options | `8` |

> Note: the **chart REST endpoints** (`/charts/*`) accept a narrower set: `NSE_EQ, NSE_FNO, BSE_EQ, BSE_FNO, MCX_COMM, IDX_I` (per the OpenAPI spec). Currency segments are not listed there.

### Product Type
| Attribute | Detail |
|---|---|
| `CNC` | Cash & Carry (equity delivery) |
| `INTRADAY` | Intraday (Equity, F&O) |
| `MARGIN` | Carry-forward in F&O |
| `CO` | Cover Order |
| `BO` | Bracket Order |

> `CO` & `BO` are valid for **intraday only**. (Python lib also defines `MTF`.)

### Order Status
| Attribute | Detail |
|---|---|
| `TRANSIT` | Did not reach exchange server |
| `PENDING` | Awaiting execution |
| `CLOSED` | Super Order: both entry & exit placed |
| `TRIGGERED` | Super Order: target or SL leg triggered |
| `REJECTED` | Rejected by broker/exchange |
| `CANCELLED` | Cancelled by user |
| `PART_TRADED` | Partially traded |
| `TRADED` | Executed |

### After-Market Order (AMO) time
| Attribute | Detail |
|---|---|
| `PRE_OPEN` | Pumped at pre-market session |
| `OPEN` | Pumped at market open |
| `OPEN_30` | 30 min after open |
| `OPEN_60` | 60 min after open |

### Expiry Code
| Code | Detail |
|---|---|
| `0` | Current / near expiry |
| `1` | Next expiry |
| `2` | Far expiry |

> The **expired-options** endpoint (`/charts/rollingoption`) restricts `expiryCode` to `1`, `2`, `3` per the OpenAPI spec.

### Instrument
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

> Chart REST endpoints accept the subset: `INDEX, FUTIDX, OPTIDX, EQUITY, FUTSTK, OPTSTK, FUTCOM, OPTFUT`.

### Feed Request Code (client → server, WS)
| Code | Action |
|---|---|
| `11` | Connect feed |
| `12` | Disconnect feed |
| `15` | Subscribe — Ticker packet |
| `16` | Unsubscribe — Ticker packet |
| `17` | Subscribe — Quote packet |
| `18` | Unsubscribe — Quote packet |
| `21` | Subscribe — Full packet |
| `22` | Unsubscribe — Full packet |
| `23` | Subscribe — Full Market Depth |
| `24` | Unsubscribe — Full Market Depth |

### Feed Response Code (server → client, WS; first/third byte of header)
| Code | Packet |
|---|---|
| `1` | Index packet |
| `2` | Ticker packet |
| `4` | Quote packet |
| `5` | OI packet |
| `6` | Prev-close packet |
| `7` | Market-status packet |
| `8` | Full packet |
| `41` | Depth — Bid (Buy) data *(20/200-level depth feed)* |
| `51` | Depth — Ask (Sell) data *(20/200-level depth feed)* |
| `50` | Feed disconnect |

### Conditional Triggers (alerts) — comparison types
| Type | Mandatory fields |
|---|---|
| `TECHNICAL_WITH_VALUE` | `indicatorName`, `operator`, `timeFrame`, `comparingValue` |
| `TECHNICAL_WITH_INDICATOR` | `indicatorName`, `operator`, `timeFrame`, `comparingIndicatorName` |
| `TECHNICAL_WITH_CLOSE` | `indicatorName`, `operator`, `timeFrame` |
| `PRICE_WITH_VALUE` | `operator`, `comparingValue` |

**Indicator names:** `SMA_5/10/20/50/100/200`, `EMA_5/10/20/50/100/200`, `BB_UPPER`, `BB_LOWER`, `RSI_14`, `ATR_14`, `STOCHASTIC`, `STOCHRSI_14`, `MACD_26`, `MACD_12`, `MACD_HIST`.

**Operators:** `CROSSING_UP`, `CROSSING_DOWN`, `CROSSING_ANY_SIDE`, `GREATER_THAN`, `LESS_THAN`, `GREATER_THAN_EQUAL`, `LESS_THAN_EQUAL`, `EQUAL`, `NOT_EQUAL`.

**Alert status:** `ACTIVE`, `TRIGGERED`, `EXPIRED`, `CANCELLED`.

---

## 6. Instrument master (scrip list)

Download the full tradable-instrument list as CSV (no auth needed for the CSV URLs):
- **Compact:** `https://images.dhan.co/api-data/api-scrip-master.csv`
- **Detailed:** `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`

Or fetch one exchange-segment at a time (auth'd):
```
GET https://api.dhan.co/v2/instrument/{exchangeSegment}
```
`{exchangeSegment}` is one of the string attributes in [§5](#exchange-segment).

### Column mapping (Detailed `tag` → Compact `tag`)
| Detailed | Compact | Description |
|---|---|---|
| `EXCH_ID` | `SEM_EXM_EXCH_ID` | Exchange: `NSE` `BSE` `MCX` |
| `SEGMENT` | `SEM_SEGMENT` | `C` Currency, `D` Derivatives, `E` Equity, `M` Commodity |
| `ISIN` | — | 12-char ISIN |
| `INSTRUMENT` | `SEM_INSTRUMENT_NAME` | See [Instrument enum](#instrument) |
| *(removed)* | `SEM_EXPIRY_CODE` | Expiry code (futures) |
| `UNDERLYING_SECURITY_ID` | — | Underlying security ID (derivatives) |
| `UNDERLYING_SYMBOL` | — | Underlying symbol (derivatives) |
| `SYMBOL_NAME` | `SM_SYMBOL_NAME` | Symbol name |
| *(removed)* | `SEM_TRADING_SYMBOL` | Exchange trading symbol |
| `DISPLAY_NAME` | `SEM_CUSTOM_SYMBOL` | Dhan display name |
| `INSTRUMENT_TYPE` | `SEM_EXCH_INSTRUMENT_TYPE` | Extra instrument detail |
| `SERIES` | `SEM_SERIES` | Exchange series |
| `LOT_SIZE` | `SEM_LOT_UNITS` | Lot size |
| `SM_EXPIRY_DATE` | `SEM_EXPIRY_DATE` | Expiry date (derivatives) |
| `STRIKE_PRICE` | `SEM_STRIKE_PRICE` | Strike price (options) |
| `OPTION_TYPE` | `SEM_OPTION_TYPE` | `CE` Call / `PE` Put |
| `TICK_SIZE` | `SEM_TICK_SIZE` | Min price decimal |
| `EXPIRY_FLAG` | `SEM_EXPIRY_FLAG` | `M` Monthly / `W` Weekly |
| `BRACKET_FLAG` | — | BO allowed: `N`/`Y` |
| `COVER_FLAG` | — | CO allowed: `N`/`Y` |
| `ASM_GSM_FLAG` | — | `N` not in ASM/GSM, `R` removed, `Y` in ASM/GSM |
| `ASM_GSM_CATEGORY` | — | Category, `NA` if none |
| `BUY_SELL_INDICATOR` | — | `A` if both buy/sell allowed |
| `BUY_CO_MIN_MARGIN_PER` / `SELL_CO_MIN_MARGIN_PER` | — | CO min-margin % (buy/sell) |
| `BUY_CO_SL_RANGE_MAX_PERC` / `SELL_CO_SL_RANGE_MAX_PERC` | — | CO SL max range % |
| `BUY_CO_SL_RANGE_MIN_PERC` / `SELL_CO_SL_RANGE_MIN_PERC` | — | CO SL min range % |
| `BUY_BO_MIN_MARGIN_PER` / `SELL_BO_MIN_MARGIN_PER` | — | BO min-margin % |
| `BUY_BO_SL_RANGE_MAX_PERC` / `SELL_BO_SL_RANGE_MAX_PERC` | — | BO SL max range % |
| `BUY_BO_SL_RANGE_MIN_PERC` / `SELL_BO_SL_MIN_RANGE` | — | BO SL min range % |
| `BUY_BO_PROFIT_RANGE_MAX_PERC` / `SELL_BO_PROFIT_RANGE_MAX_PERC` | — | BO target max range % |
| `BUY_BO_PROFIT_RANGE_MIN_PERC` / `SELL_BO_PROFIT_RANGE_MIN_PERC` | — | BO target min range % |
| `MTF_LEVERAGE` | — | MTF leverage (x) for eligible equity |

> `SecurityId` (used everywhere in the APIs) comes from this list. Cache the CSV locally and refresh daily (it changes with expiries/listings).

---

## 7. Historical Data API (REST)

OHLC + Volume (+ optional OI) candles. Two endpoints:
| Method | Path | Use |
|---|---|---|
| `POST` | `/charts/historical` | Daily timeframe OHLC |
| `POST` | `/charts/intraday` | Minute-timeframe OHLC |

Both return arrays-of-columns (column-oriented), not row objects. Arrays are positionally aligned: `open[i]`, `high[i]`, … share `timestamp[i]`.

### 7a. Daily historical — `POST /charts/historical`
Data available **back to the instrument's inception date**.
```
POST https://api.dhan.co/v2/charts/historical
Headers: Content-Type: application/json, access-token: JWT
```
Request:
```json
{
  "securityId": "1333",
  "exchangeSegment": "NSE_EQ",
  "instrument": "EQUITY",
  "expiryCode": 0,
  "oi": false,
  "fromDate": "2022-01-08",
  "toDate": "2022-02-08"
}
```
| Field | Type | Req | Notes |
|---|---|---|---|
| `securityId` | string | ✓ | From instrument master |
| `exchangeSegment` | enum string | ✓ | See [§5](#exchange-segment) (chart subset) |
| `instrument` | enum string | ✓ | See [Instrument](#instrument) (chart subset) |
| `expiryCode` | enum int | optional | For derivatives; `0`/`1`/`2` |
| `oi` | boolean | optional | Include Open Interest (F&O) |
| `fromDate` | string `YYYY-MM-DD` | ✓ | Start (inclusive) |
| `toDate` | string `YYYY-MM-DD` | ✓ | End (**non-inclusive**) |

Response: `{ open[], high[], low[], close[], volume[], timestamp[], open_interest[] }` — all numeric arrays. `timestamp` = epoch seconds (see [§13](#13-gotchas--implementation-notes)).

### 7b. Intraday historical — `POST /charts/intraday`
1/5/15/25/60-minute candles for **the last ~5 years**, all exchanges/segments, all active instruments.
```
POST https://api.dhan.co/v2/charts/intraday
Headers: Accept: application/json, Content-Type: application/json, access-token:
```
Request:
```json
{
  "securityId": "1333",
  "exchangeSegment": "NSE_EQ",
  "instrument": "EQUITY",
  "interval": "1",
  "oi": false,
  "fromDate": "2024-09-11 09:30:00",
  "toDate": "2024-09-15 13:00:00"
}
```
| Field | Type | Req | Notes |
|---|---|---|---|
| `securityId` | string | ✓ | |
| `exchangeSegment` | enum string | ✓ | |
| `instrument` | enum string | ✓ | |
| `interval` | enum int-as-string | ✓ | `"1"`, `"5"`, `"15"`, `"25"`, `"60"` |
| `oi` | boolean | optional | |
| `fromDate` | string | ✓ | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS` (IST) |
| `toDate` | string | ✓ | same formats |

**Limit:** only **90 days of data per call** for any interval — paginate and store locally. Response shape identical to daily (`open/high/low/close/volume/timestamp/open_interest`).

---

## 8. Expired Options Data API (REST)

| Method | Path | Use |
|---|---|---|
| `POST` | `/charts/rollingoption` | Continuous (rolling) expired-options contract data |

Pre-processed **rolling** expired-options data: you request by **strike relative to spot** (ATM, ATM±n) instead of hunting for expired contract security IDs. Minute-level, **up to 5 years** back, Index **and** Stock options. **Max 30 days per call.** Returns OHLC, IV, volume, OI, strike and spot.

```
POST https://api.dhan.co/v2/charts/rollingoption
Headers: Accept: application/json, Content-Type: application/json, access-token:
```
Request:
```json
{
  "exchangeSegment": "NSE_FNO",
  "interval": "1",
  "securityId": 13,
  "instrument": "OPTIDX",
  "expiryFlag": "MONTH",
  "expiryCode": 1,
  "strike": "ATM",
  "drvOptionType": "CALL",
  "requiredData": ["open","high","low","close","volume"],
  "fromDate": "2021-08-01",
  "toDate": "2021-09-01"
}
```
| Field | Type | Req | Notes |
|---|---|---|---|
| `exchangeSegment` | enum string | ✓ | chart subset |
| `interval` | enum int-as-string | ✓ | `"1"/"5"/"15"/"25"/"60"` |
| `securityId` | int (underlying) | ✓ | Underlying's security ID (e.g. 13 = Nifty) |
| `instrument` | enum string | ✓ | e.g. `OPTIDX` |
| `expiryFlag` | enum string | ✓ | `WEEK` or `MONTH` |
| `expiryCode` | enum int | ✓ | per OpenAPI: `1`/`2`/`3` |
| `strike` | enum string | ✓ | `ATM`; up to `ATM+10`/`ATM-10` for index near-expiry; up to `ATM+3`/`ATM-3` otherwise |
| `drvOptionType` | enum string | ✓ | `CALL` or `PUT` |
| `requiredData` | array string | ✓ | any of `open` `high` `low` `close` `iv` `volume` `strike` `oi` `spot` |
| `fromDate` | string `YYYY-MM-DD` | ✓ | inclusive |
| `toDate` | string `YYYY-MM-DD` | ✓ | **non-inclusive** |

Response (only the requested arrays are populated; the other branch is `null` when you query one option type):
```json
{
  "data": {
    "ce": { "iv":[], "oi":[], "strike":[], "spot":[], "open":[354,360.3],
            "high":[], "low":[], "close":[], "volume":[],
            "timestamp":[1756698300,1756699200] },
    "pe": null
  }
}
```
Response array element types (from OpenAPI): `iv/strike/spot/open/high/low/close` = double; `oi/volume/timestamp` = int64. `timestamp` = unix seconds.

> Note: the example request uses `drvOptionType:"CALL"` yet returns both `ce`/`pe` keys with `pe:null`. The response always carries both keys; the non-requested side is `null`.

---

## 9. Option Chain API (REST)

| Method | Path | Use |
|---|---|---|
| `POST` | `/optionchain` | Full option chain for an underlying+expiry |
| `POST` | `/optionchain/expirylist` | List of active expiry dates for an underlying |

Covers NSE, BSE and MCX options. Returns OI, Greeks, volume, top bid/ask and price for **every strike**.

> **Rate limit: 1 unique request / 3 seconds** (OI updates slowly). You can request different underlyings or different expiries of the same underlying concurrently within that cadence.
> Both endpoints require **both** `access-token` **and** `client-id` headers.

### 9a. Option chain — `POST /optionchain`
```
POST https://api.dhan.co/v2/optionchain
Headers: Content-Type: application/json, access-token: JWT, client-id: 1000000001
```
Request:
```json
{ "UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": "2024-10-31" }
```
| Field | Type | Notes |
|---|---|---|
| `UnderlyingScrip` | int (required) | Security ID of underlying (note: docs sometimes label `UnderlyingScri`) |
| `UnderlyingSeg` | enum string | Exchange/segment of underlying ([§5](#exchange-segment)) |
| `Expiry` | string `YYYY-MM-DD` | One expiry; get valid ones from the expiry-list call |

Response (truncated; `oc` is a map keyed by strike string like `"25650.000000"`, each with `ce` and `pe`):
```json
{
  "data": {
    "last_price": 25642.8,
    "oc": {
      "25650.000000": {
        "ce": {
          "average_price": 146.99,
          "greeks": { "delta": 0.53871, "theta": -15.1539, "gamma": 0.00132, "vega": 12.18593 },
          "implied_volatility": 9.789193798280868,
          "last_price": 134,
          "oi": 3786445,
          "previous_close_price": 244.85,
          "previous_oi": 402220,
          "previous_volume": 31931705,
          "security_id": 42528,
          "top_ask_price": 134, "top_ask_quantity": 1365,
          "top_bid_price": 133.55, "top_bid_quantity": 1625,
          "volume": 117567970
        },
        "pe": { "...": "same shape" }
      }
    }
  },
  "status": "success"
}
```
**Per-side (ce/pe) fields:** `average_price` (float), `greeks.delta/theta/gamma/vega` (float), `implied_volatility` (float), `last_price` (float), `oi` (int), `previous_close_price` (float), `previous_oi` (int), `previous_volume` (int), `security_id` (int), `top_ask_price` (float), `top_ask_quantity` (int), `top_bid_price` (float), `top_bid_quantity` (int), `volume` (int).
Greeks: delta = ₹ premium change per ₹1 underlying move; theta = time decay; gamma = rate of change of delta; vega = premium change per 1% IV change.

> `average_price` and `security_id` were **added in v2.5**.

### 9b. Expiry list — `POST /optionchain/expirylist`
```
POST https://api.dhan.co/v2/optionchain/expirylist
Headers: Content-Type: application/json, access-token: JWT, client-id: 1000000001
```
Request:
```json
{ "UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I" }
```
Response: `{ "data": ["2024-10-17","2024-10-24", ...], "status": "success" }` — array of `YYYY-MM-DD`.

---

## 10. Live Market Feed (WebSocket, binary)

Tick-by-tick, event-based market data over WebSocket. **Requests are JSON; responses are binary, little-endian.** Use a WS library + a binary parser.

- **Up to 5 WS connections per user**, **5000 instruments per connection**.
- **Max 100 instruments per subscription JSON message** (send multiple messages to reach 5000).

### Connect
```
wss://api-feed.dhan.co?version=2&token=eyxxxxx&clientId=100xxxxxxx&authType=2
```
Query params: `version`=`2` (required), `token`=access token (required), `clientId` (required), `authType`=`2` (required default).

### Subscribe (JSON → server)
```json
{
  "RequestCode": 15,
  "InstrumentCount": 2,
  "InstrumentList": [
    { "ExchangeSegment": "NSE_EQ", "SecurityId": "1333" },
    { "ExchangeSegment": "BSE_EQ", "SecurityId": "532540" }
  ]
}
```
`RequestCode` selects the mode (see [Feed Request Code](#feed-request-code-client--server-ws): 15 Ticker, 17 Quote, 21 Full, etc.). `InstrumentCount` = number in this message (≤100). `ExchangeSegment` string + `SecurityId` string per instrument.

### Keep-alive
Server sends a **ping every 10 s**; your WS library auto-replies with pong. **No response for >40 s → server closes the connection** and you must reconnect.

### Disconnect (JSON → server)
```json
{ "RequestCode": 12 }
```

### Binary response — common header (8 bytes)
| Bytes | Type | Size | Field |
|---|---|---|---|
| `1` | byte | 1 | **Feed Response Code** ([table](#feed-response-code-server--client-ws-firstthird-byte-of-header)) |
| `2-3` | int16 | 2 | Message length of the entire payload |
| `4` | byte | 1 | Exchange Segment (int enum) |
| `5-8` | int32 | 4 | Security ID |

> ⚠️ This header order (code first) is **different** from the Depth-feed header in [§11](#11-full-market-depth--20--200-level-websocket-binary) (length first). Don't reuse the parser.

### Ticker packet — response code `2` (16 bytes total)
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 2) | |
| `9-12` | float32 | Last Traded Price (LTP) |
| `13-16` | int32 | Last Trade Time (epoch) |

### Prev-close packet — response code `6` (sent automatically on subscribe)
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 6) | |
| `9-12` | float32 | Previous-day close price |
| `13-16` | int32 | Previous-day Open Interest |

### Quote packet — response code `4` (50 bytes total)
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 4) | |
| `9-12` | float32 | LTP |
| `13-14` | int16 | Last Traded Quantity |
| `15-18` | int32 | Last Trade Time (epoch) |
| `19-22` | float32 | Average Trade Price (ATP) |
| `23-26` | int32 | Volume |
| `27-30` | int32 | Total Sell Quantity |
| `31-34` | int32 | Total Buy Quantity |
| `35-38` | float32 | Day Open |
| `39-42` | float32 | Day Close (post-close only) |
| `43-46` | float32 | Day High |
| `47-50` | float32 | Day Low |

### OI packet — response code `5` (sent alongside Quote for derivatives)
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 5) | |
| `9-12` | int32 | Open Interest |

### Full packet — response code `8` (163 bytes: 63 + 100 depth)
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 8) | |
| `9-12` | float32 | LTP |
| `13-14` | int16 | Last Traded Quantity |
| `15-18` | int32 | Last Trade Time (epoch) |
| `19-22` | float32 | ATP |
| `23-26` | int32 | Volume |
| `27-30` | int32 | Total Sell Quantity |
| `31-34` | int32 | Total Buy Quantity |
| `35-38` | int32 | Open Interest |
| `39-42` | int32 | Day-high OI (NSE_FNO only) |
| `43-46` | int32 | Day-low OI (NSE_FNO only) |
| `47-50` | float32 | Day Open |
| `51-54` | float32 | Day Close (post-close only) |
| `55-58` | float32 | Day High |
| `59-62` | float32 | Day Low |
| `63-162` | Market-depth block | 100 bytes = **5 levels × 20 bytes** |

**5-level depth sub-packet (20 bytes each):**
| Bytes | Type | Field |
|---|---|---|
| `1-4` | int32 | Bid Quantity |
| `5-8` | int32 | Ask Quantity |
| `9-10` | int16 | No. of Bid Orders |
| `11-12` | int16 | No. of Ask Orders |
| `13-16` | float32 | Bid Price |
| `17-20` | float32 | Ask Price |

### Feed disconnect packet — response code `50`
| Bytes | Type | Field |
|---|---|---|
| `0-8` | header (code 50) | |
| `9-10` | int16 | Disconnection reason code (see [8xx codes](#data-api-errors--websocket-disconnect-codes-8xx)) |

> If a 6th+ WS connection opens, the **oldest** connection is dropped with code `805`.

---

## 11. Full Market Depth — 20 & 200 level (WebSocket, binary)

Level-3 depth: **20 levels** and **200 levels**, streamed real-time. **NSE Equity & Derivatives segments only.** Requests JSON, responses binary little-endian.

### Connect
- 20-level: `wss://depth-api-feed.dhan.co/twentydepth?token=eyxxxxx&clientId=100xxxxxxx&authType=2`
- 200-level: `wss://full-depth-api.dhan.co/twohundreddepth?token=eyxxxxx&clientId=100xxxxxxx&authType=2`

Query params: `token`, `clientId`, `authType=2`.

### Subscribe
**20-level — up to 50 instruments per connection** (all 50 can go in one message, or split):
```json
{
  "RequestCode": 23,
  "InstrumentCount": 1,
  "InstrumentList": [ { "ExchangeSegment": "NSE_EQ", "SecurityId": "1333" } ]
}
```
**200-level — only 1 instrument per connection**:
```json
{ "RequestCode": 23, "ExchangeSegment": "NSE_EQ", "SecurityId": "1333" }
```
`RequestCode` = `23` (Subscribe Full Market Depth) in both cases.

### Keep-alive
Same ping/pong as §10: ping every 10 s, disconnect after >40 s of silence.

### Depth response — header (12 bytes) — **note different order vs §10**
| Bytes | Type | Field |
|---|---|---|
| `1-2` | int16 | Message length of entire payload |
| `3` | byte | Feed Response Code (`41` Bid/Buy, `51` Ask/Sell) |
| `4` | byte | Exchange Segment |
| `5-8` | int32 | Security ID |
| `9-12` | uint32 | **20-level:** message sequence (ignore). **200-level:** number of rows to read |

### 20-level depth packet
Bid and Ask arrive in **separate** packets (codes 41 / 51), each carrying **20 entries × 16 bytes = 320 bytes**.
- `0-12`: header. `13-332`: 20 × 16-byte entries.

### 200-level depth packet
Same structure scaled up: **200 entries × 16 bytes = 3200 bytes** per packet.
- `0-12`: header. `13-3212`: 200 × 16-byte entries.

### 16-byte depth entry (both 20 & 200 level)
| Bytes | Type | Field |
|---|---|---|
| `1-8` | **float64** | Price |
| `9-12` | uint32 | Quantity |
| `13-16` | uint32 | No. of Orders |

> ⚠️ Depth prices are **float64** here (vs float32 in the §10 5-level block).
> **Packet stacking:** when multiple instruments are subscribed (20-level), packets are concatenated in one message as: instrument-1 Bid, instrument-1 Ask, instrument-2 Bid, instrument-2 Ask, … Split by length (header `1-2`) to decode.

### Disconnect packet — response code `50`
| Bytes | Type | Field |
|---|---|---|
| `0-12` | header (code 50) | |
| `13-14` | int16 | Disconnection reason code ([8xx](#data-api-errors--websocket-disconnect-codes-8xx)) |

---

## 12. Live Order Update (WebSocket, JSON)

Real-time updates for **all** your orders, regardless of which platform placed them. **Messages are JSON** (not binary).

### Connect & authorise
```
wss://api-order-update.dhan.co
```
Immediately send an authorisation message.

**Individual:**
```json
{ "LoginReq": { "MsgCode": 42, "ClientId": "1000000001", "Token": "JWT" }, "UserType": "SELF" }
```
**Partner:**
```json
{ "LoginReq": { "MsgCode": 42, "ClientId": "partner_id" }, "UserType": "PARTNER", "Secret": "partner_secret" }
```
`MsgCode` = `42` (default). `UserType` = `SELF` (individual) or `PARTNER`.

### Order-update message (`Type: "order_alert"`, payload in `Data`)
Selected fields (full set below):
| Field | Type | Notes |
|---|---|---|
| `Exchange` | string | NSE/BSE/MCX |
| `Segment` | string | Order segment |
| `Source` | string | Platform; `P` for API orders (`N` seen in sample) |
| `SecurityId` | string | |
| `ClientId` | string | |
| `ExchOrderNo` | string | Exchange order id |
| `OrderNo` | string | Dhan order id |
| `Product` | enum string | `C` CNC, `I` INTRADAY, `M` MARGIN, `F` MTF, `V` CO, `B` BO |
| `TxnType` | enum string | `B` Buy / `S` Sell |
| `OrderType` | enum string | `LMT` Limit, `MKT` Market, `SL` Stop-Loss, `SLM` Stop-Loss-Market |
| `Validity` | enum string | `DAY` / `IOC` |
| `DiscQuantity` | int | Disclosed qty |
| `DiscQtyRem` | int | Disclosed qty pending |
| `RemainingQuantity` | int | Pending qty |
| `Quantity` | int | Total order qty |
| `TradedQty` | int | Executed qty |
| `Price` | float | Order price |
| `TriggerPrice` | float | For SL-M/SL-L/CO/BO |
| `TradedPrice` | float | Executed price |
| `AvgTradedPrice` | float | Avg (differs from TradedPrice on partial fills) |
| `AlgoOrdNo` | float | Entry-leg order no. tracking target/SL (BO/CO) |
| `OffMktFlag` | string | `1` if AMO else `0` |
| `OrderDateTime` | string | Received by Dhan |
| `ExchOrderTime` | string | Placed on exchange |
| `LastUpdatedTime` | string | Last modify/trade time |
| `Remarks` | string | Free text; `Super Order` if part of a super order |
| `MktType` | string | `NL` normal; `AU`/`A1`/`A2` auction |
| `ReasonDescription` | string | Rejection reason |
| `LegNo` | int | `1` Entry, `2` SL, `3` Target |
| `Instrument` | string | See [Instrument](#instrument) |
| `Symbol` | string | |
| `ProductName` | string | See [Product Type](#product-type) |
| `Status` | enum string | `TRANSIT` `PENDING` `REJECTED` `CANCELLED` `TRADED` `EXPIRED` |
| `LotSize` | int | |
| `StrikePrice` | float | |
| `ExpiryDate` | string | |
| `OptType` | string | `CE`/`PE` (`XX` if N/A) |
| `DisplayName` | string | |
| `Isin` | string | |
| `Series` | string | |
| `GoodTillDaysDate` | string | For Forever Orders |
| `RefLtp` | float | LTP at update time |
| `TickSize` | float | |
| `AlgoId` | string | Exchange id for special orders |
| `Multiplier` | int | Commodity/currency |
| `CorrelationId` | string | Your tracking id; max 30 chars; allowed `[^a-zA-Z0-9 _-]` |

> `CorrelationId` and the order-level `Remarks` were added in v2.2.

---

## 13. Gotchas & implementation notes

1. **Timestamp epoch — conflicting docs.** Release notes (v2) say "Epoch/UNIX time" and the Python lib's `convert_to_date_time` uses `datetime.fromtimestamp(epoch, IST)` → standard **Unix epoch (seconds since 1970-01-01 UTC)**. The OpenAPI spec text says "seconds since January 01, 1980" — **treat that as a spec error**; use Unix-1970. Always convert to **IST (UTC+5:30)** for display. (The example arrays in the docs use mismatched placeholder dates — don't infer the epoch from them.)
2. **Binary endianness:** all WS payloads are **little-endian**. Set endianness explicitly if your host is big-endian.
3. **Two different WS headers:** Live-Feed header = `[code(1)][len(2)][seg(1)][secId(4)]` (8 bytes). Depth header = `[len(2)][code(1)][seg(1)][secId(4)][seq/rows(4)]` (12 bytes). Separate parsers.
4. **Depth price width:** 5-level depth inside the Full packet uses **float32**; 20/200-level depth entries use **float64**.
5. **`toDate` inclusivity:** Daily history and Expired-options `toDate` are **non-inclusive**; Intraday is a date-time range (treat the end bound as inclusive of bars up to that time). Test empirically at boundaries.
6. **Data volume caps per call:** Intraday history **≤90 days/call**; Expired options **≤30 days/call**. Both reach back ~5 years. Paginate and persist locally.
7. **Option Chain throttle:** **1 request / 3 s**; needs both `access-token` + `client-id` headers; `oc` is a **map keyed by a float-string strike** (`"25650.000000"`), not a list — iterate keys.
8. **Subscription chunking (Live Feed):** ≤100 instruments per JSON message, ≤5000/connection, ≤5 connections/user. Opening a 6th drops the oldest with code `805`.
9. **Full Market Depth scope:** **NSE EQ & FNO only.** 20-level ≤50 instruments/conn; **200-level = 1 instrument/conn**.
10. **Static IP:** required for **order placement** (Orders/Super/Forever) only; **not** for reads. Locked 7 days after set.
11. **Token lifetime:** 24h (web or API-generated). Build auto-renew: web tokens via `/RenewToken`; API-key flow re-runs consent; TOTP flow re-calls `generateAccessToken`.
12. **Exchange-segment dual encoding:** string in JSON (`NSE_EQ`), integer in binary packets and in `MarketFeed`/`FullDepth` constants (`NSE = 1`). Map carefully.
13. **Library subscription constants vs documented codes:** the Python `MarketFeed` exposes `Ticker=15, Quote=17, Depth=19, Full=21`. The Annexure documents `15/17/21` and uses **`23`** for Full Market Depth; `19` is the lib's internal "Depth" mode constant. When working at the raw-protocol level, follow the **Annexure** request codes; when using the SDK, use the SDK constants.
14. **Order modification semantics (v2 breaking change):** `quantity` in Order Modify must be the **originally placed** quantity, not the pending quantity.
15. **`PART_TRADED`:** distinct order-status flag for partial fills (don't treat as fully traded).

---

## 14. Python SDK (`dhanhq`) reference

- **Latest version:** `2.2.0` (2026-04-24). **Last stable pre-2.1 refactor:** `2.0.2` (`pip install dhanhq==2.0.2`).
- **License:** MIT. **Author:** Dhan (`dhan-oss@dhan.co`). **Repo:** `https://github.com/dhan-oss/DhanHQ-py` (layout under `src/dhanhq/`).
- **Runtime deps:** `pandas>=1.4.3`, `requests>=2.28.1`, `websockets>=12.0.1`, `pyOpenSSL>=20.0.1`.
- **Python docs:** `https://dhanhq.co/docs/DhanHQ-py/`.

```bash
pip install dhanhq
```

### Package structure & public exports
`src/dhanhq/__init__.py` exports:
`DhanContext`, `DhanLogin`, `DhanHTTP`, `Order`, `ForeverOrder`, `SuperOrder`, `Portfolio`, `Funds`, `Statement`, `TraderControl`, `Security`, `HistoricalData`, `OptionChain`, `MarketFeed` (REST quote mixin **and** the WS class — see note), `dhanhq`, `OrderUpdate`, `FullDepth`.

> The name `MarketFeed` is bound twice: the internal REST-quote mixin `_market_feed.MarketFeed` and the WebSocket `marketfeed.MarketFeed`. The final `from .marketfeed import MarketFeed` wins, so **`MarketFeed` = the WebSocket class**. The composite `dhanhq` class inherits REST quote methods from the internal mixin directly.

### `DhanContext` — credentials holder (define once)
```python
DhanContext(client_id, access_token, disable_ssl=False, pool=None)
```
Methods: `get_client_id()`, `get_access_token()`, `get_dhan_http()`, `get_dhan_login()`.

### `DhanLogin` — auth / token / IP management
```python
DhanLogin(client_id)
  .generate_login_session(app_id, app_secret)          # OAuth step 1 → consent + opens browser
  .consume_token_id(token_id, app_id, app_secret)      # OAuth step 3 → access_token
  .generate_token(pin, totp)                           # PIN+TOTP flow
  .renew_token(access_token)
  .user_profile(access_token)
  .set_ip(access_token, ip, ip_flag, dhan_client_id=None)
  .modify_ip(access_token, ip, ip_flag, dhan_client_id=None)
  .get_ip(access_token, dhan_client_id=None)
```

### `dhanhq` — core composite client
```python
from dhanhq import DhanContext, dhanhq
ctx = DhanContext("client_id", "access_token")
dhan = dhanhq(ctx)
```
**Class constants:**
- Exchange: `NSE='NSE_EQ'`, `BSE='BSE_EQ'`, `CUR='NSE_CURRENCY'`, `MCX='MCX_COMM'`, `FNO='NSE_FNO'`, `NSE_FNO='NSE_FNO'`, `BSE_FNO='BSE_FNO'`, `INDEX='IDX_I'`
- Txn: `BUY='BUY'`, `SELL='SELL'`
- Product: `CNC='CNC'`, `INTRA='INTRADAY'`, `MARGIN='MARGIN'`, `CO='CO'`, `BO='BO'`, `MTF='MTF'`
- Order type: `LIMIT='LIMIT'`, `MARKET='MARKET'`, `SL='STOP_LOSS'`, `SLM='STOP_LOSS_MARKET'`
- Validity: `DAY='DAY'`, `IOC='IOC'`

**Data / market methods:**
```python
dhan.historical_daily_data(security_id, exchange_segment, instrument_type, from_date, to_date, expiry_code=0, oi=False)
dhan.intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date, interval=1, oi=False)
dhan.expired_options_data(security_id, exchange_segment, instrument_type, expiry_flag, expiry_code,
                          strike, drv_option_type, required_data, from_date, to_date, interval=1)
dhan.option_chain(under_security_id, under_exchange_segment, expiry)
dhan.expiry_list(under_security_id, under_exchange_segment)
dhan.ticker_data(securities)     # LTP   — securities = {"NSE_EQ":[1333], ...}
dhan.ohlc_data(securities)       # OHLC
dhan.quote_data(securities)      # full quote packet
dhan.fetch_security_list(mode='compact', filename='security_id_list.csv')   # downloads scrip master
dhan.convert_to_date_time(epoch) # epoch → IST datetime/date
```

**Order / portfolio / funds methods:**
```python
dhan.place_order(security_id, exchange_segment, transaction_type, quantity, order_type, product_type,
                 price, trigger_price=0, disclosed_quantity=0, after_market_order=False,
                 validity='DAY', amo_time='OPEN', bo_profit_value=None, bo_stop_loss_Value=None,
                 tag=None, should_slice=False)
dhan.place_slice_order(... same as place_order minus should_slice ...)
dhan.modify_order(order_id, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)
dhan.cancel_order(order_id)
dhan.get_order_list()
dhan.get_order_by_id(order_id)
dhan.get_order_by_correlationID(correlation_id)
dhan.get_trade_book(order_id)                       # (README shows get_trade_book; see note)
dhan.get_trade_history(from_date, to_date, page_number=0)
dhan.get_positions()
dhan.get_holdings()
dhan.convert_position(from_product_type, exchange_segment, position_type, security_id, convert_qty, to_product_type)
dhan.get_fund_limits()
dhan.margin_calculator(security_id, exchange_segment, transaction_type, quantity, product_type, price, trigger_price=0)
# Super Orders
dhan.place_super_order(security_id, exchange_segment, transaction_type, quantity, order_type, product_type,
                       price, targetPrice=0.0, stopLossPrice=0.0, trailingJump=0.0, tag=None)
dhan.modify_super_order(order_id, order_type, leg_name, quantity=0, price=0.0, targetPrice=0.0,
                        stopLossPrice=0.0, trailingJump=0.0)
dhan.cancel_super_order(order_id, order_leg)
dhan.get_super_order_list()
# Forever Orders
dhan.place_forever(security_id, exchange_segment, transaction_type, product_type, order_type, quantity,
                   price, trigger_Price, order_flag="SINGLE", disclosed_quantity=0, validity='DAY',
                   price1=0, trigger_Price1=0, quantity1=0, tag=None, symbol="")
dhan.modify_forever(order_id, order_flag, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)
dhan.get_forever()
dhan.cancel_forever(order_id)
# eDIS (to sell holdings via CDSL eDIS)
dhan.generate_tpin()
dhan.open_browser_for_tpin(isin, qty, exchange, segment='EQ', bulk=False)
dhan.edis_inquiry(isin)
```
> The composite also inherits `Statement` (ledger) and `TraderControl` (kill switch / PnL exit) mixins; see [§15](#15-appendix--full-rest-endpoint-inventory) for the underlying endpoints.

### `MarketFeed` — live feed WebSocket
```python
from dhanhq import DhanContext, MarketFeed
ctx = DhanContext("client_id", "access_token")
instruments = [(MarketFeed.NSE, "1333", MarketFeed.Ticker),
               (MarketFeed.NSE, "1333", MarketFeed.Quote),
               (MarketFeed.NSE, "1333", MarketFeed.Full)]
feed = MarketFeed(ctx, instruments, version="v2")
feed.run_forever()
while True:
    print(feed.get_data())
# control:
feed.subscribe_symbols([(MarketFeed.NSE, "14436", MarketFeed.Ticker)])
feed.unsubscribe_symbols([(MarketFeed.NSE, "1333", 16)])   # 16 = unsubscribe ticker
feed.close_connection()
```
- Constructor: `MarketFeed(dhan_context, instruments, version='v2', on_connect=None, on_message=None, on_close=None, on_error=None, on_ticks=None)`.
- Subscription tuple: `(exchange_segment_int, "security_id", subscription_type)`. If `subscription_type` omitted → defaults to Ticker.
- **Class constants** — Exchange: `IDX=0, NSE=1, NSE_FNO=2, NSE_CURR=3, BSE=4, MCX=5, BSE_CURR=7, BSE_FNO=8`. Subscription type: `Ticker=15, Quote=17, Depth=19, Full=21`.

### `FullDepth` — 20/200-level depth WebSocket
```python
from dhanhq import DhanContext, FullDepth
ctx = DhanContext(client_id, access_token)
instruments = [(1, "1333")]      # (exchange_segment_int, security_id); up to 50 for 20-depth
depth = FullDepth(ctx, instruments, depth_level=200)   # 20 or 200 (default 20)
depth.run_forever()
while True:
    depth.get_data()
    if depth.on_close:
        print("Server disconnection detected.")
        break
```
- Constructor: `FullDepth(dhan_context, instruments, depth_level=20)`. Constants: `NSE=1, NSE_FNO=2`.
- Methods include `subscribe_symbols`, `unsubscribe_symbols`, `close_connection`.

### `OrderUpdate` — live order updates WebSocket
```python
from dhanhq import DhanContext, OrderUpdate
import time
ctx = DhanContext("client_id", "access_token")

def on_order_update(order_data: dict):
    print(order_data["Data"])

client = OrderUpdate(ctx)
client.on_update = on_order_update      # optional callback
while True:
    try:
        client.connect_to_dhan_websocket_sync()
    except Exception as e:
        print(f"reconnecting in 5s: {e}")
        time.sleep(5)
```
- Constructor: `OrderUpdate(dhan_context)`. Methods: `connect_to_dhan_websocket_sync()`, `handle_order_update(order_update)`, plus the assignable `on_update` callback.

### `DhanHTTP` — low-level HTTP (rarely used directly)
- Constants: `API_BASE_URL='https://api.dhan.co/v2'`, `HTTP_DEFAULT_TIME_OUT=60`.
- Methods: `get(endpoint)`, `post(endpoint, payload)`, `put(endpoint, payload)`, `delete(endpoint)`.

> **v2.1.0 import/init changes (still current):** use `from dhanhq import dhanhq/MarketFeed/OrderUpdate` (no submodule paths); constants moved onto classes (`MarketFeed.NSE`, `MarketFeed.Ticker`); pass a `DhanContext` instead of raw `client_id`/`access_token` strings to every class.

---

## 15. Appendix — full REST endpoint inventory

Every path in the OpenAPI spec (`/v2/v3/api-docs`), grouped. The chart/option/feed details above are the ones in your URL list; the rest are included for completeness so Claude Code has the full surface for later phases.

**IP setup:** `PUT /ip/modifyIP`, `POST /ip/setIP`, `GET /ip/getIP`
**Orders:** `POST /orders`, `GET /orders`, `GET /orders/{order-id}`, `PUT /orders/{order-id}`, `DELETE /orders/{order-id}`, `POST /orders/slicing`, `GET /orders/external/{correlation-id}`
**Trades:** `GET /trades`, `GET /trades/{order-id}`, `GET /trades/{from-date}/{to-date}/{page-number}`
**Super orders:** `POST /super/orders`, `GET /super/orders`, `PUT /super/orders/{order-id}`, `DELETE /super/orders/{order-id}/{order-leg}`
**Forever orders:** `POST /forever/orders`, `GET /forever/orders`, `PUT /forever/orders/{order-id}`, `DELETE /forever/orders/{order-id}`
**Conditional-trigger alerts:** `POST /alerts/orders`, `GET /alerts/orders`, `GET /alerts/orders/{alertId}`, `PUT /alerts/orders/{alertId}`, `DELETE /alerts/orders/{alertId}`
**Positions / holdings:** `GET /positions`, `DELETE /positions` (exit all), `POST /positions/convert`, `GET /holdings`
**eDIS:** `POST /edis/form`, `POST /edis/bulkform`, `GET /edis/tpin`, `GET /edis/inquire/{isin}`
**Trader's control:** `GET /pnlExit`, `POST /pnlExit`, `DELETE /pnlExit`, `GET /killswitch`, `POST /killswitch`
**Funds / margin / ledger:** `POST /margincalculator`, `POST /margincalculator/multi`, `GET /fundlimit`, `GET /ledger`
**Charts (data):** `POST /charts/historical`, `POST /charts/intraday`, `POST /charts/rollingoption`
**Auth (separate host `auth.dhan.co`):** `/app/generateAccessToken`, `/app/generate-consent`, `/login/consentApp-login`, `/app/consumeApp-consent`, `/partner/generate-consent`, `/consent-login`, `/partner/consume-consent`; **`api.dhan.co`**: `/RenewToken`, `/profile`
**Option chain (not in the OpenAPI spec but live & documented):** `POST /optionchain`, `POST /optionchain/expirylist`
**Market quote (not in your URL list; supported by SDK):** LTP / OHLC / full-quote snapshot endpoints surfaced via `dhan.ticker_data/ohlc_data/quote_data`.

### Version timeline (release notes)
- **v2.5** (2026-02-09): Conditional Trigger Orders; P&L-based exit + Exit-All API (Trader's Control); access-token generation via API (TOTP); Option Chain rate-limit boost + `average_price`/`security_id` fields.
- **v2.4** (2025-09-22): API-key login; **24h token cap**; **mandatory static IP** for order APIs.
- **v2.3** (2025-09-08): 200-level depth on WS; historical expired-options data; order rate limit → 10/sec.
- **v2.2** (2025-03-07): Super Orders; User Profile API; 5-year intraday history + OI; `CorrelationId` on order updates; data-API rate-limit changes.
- **v2.1** (2025-01-06): 20-level depth; Option Chain API; `expiryCode` optional in daily history.
- **v2** (2024-09-15): Market Quote; Forever Orders; Live Order Update; Margin Calculator; FULL packet; **epoch (not Julian) timestamps**; `securityId` replaces `symbol` in daily history; DH-9xx error series.

---

*End of reference. If a behaviour here ever disagrees with the running API, the live API wins — re-pull `https://api.dhan.co/v2/v3/api-docs` and the docs pages, and update §13.*

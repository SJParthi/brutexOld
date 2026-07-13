# GDFL REST API — own-words summary

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> **Source of every fact on this page:** the vendor's own REST API reference
> document, captured 2026-07-13 from PyPI as the `gfdl-rest@1.0.3` long-description
> (author `Global Datafeeds <developer@globaldatafeeds.in>`), cross-checked against
> the package's shipped source code — **summarized in our own words** per the
> SUMMARIZE-ONLY ruling (`../README.md`). Endpoint/parameter/field NAMES and enum
> tokens are vendor identifiers kept verbatim; no vendor prose is reproduced.
> Re-fetch the verbatim original via the pinned digests in `../sdk/PACKAGES.md`.

## Transport + auth model
*(source: vendor REST reference captured from PyPI gfdl-rest@1.0.3, summarized; code-verified in `gfdl_rest/base_api.py`)*

- Base URL is a **customer-issued** `http://endpoint:port/` or
  `https://endpoint:port/` — GDFL issues the host + port per subscriber; **no
  hostname or port is baked into any SDK**.
- Every call is a plain HTTP **GET** at `{base_url}{FunctionName}/` (e.g.
  `GetHistory/`, `GetLastQuote/`).
- Auth = the **`accessKey`** query-string parameter appended to every GET. No
  headers, no OAuth, no session, no TOTP, no daily mint — a static key.
- Response format: JSON by default; the optional query params `xml=true` and
  `format=CSV` switch to XML / CSV.
- The SDK surfaces HTTP-level failures via `requests.raise_for_status()`; the
  captured reference documents no structured application-error envelope beyond
  that (NOT STATED IN SDK).

## Response envelope shape
*(source: vendor REST reference captured from PyPI gfdl-rest@1.0.3, summarized)*

- REST responses use **UPPERCASE keys** (`LASTTRADEPRICE`, `OPENINTEREST`, …) —
  the WebSocket twin of the same records uses CamelCase.
- History responses are wrapped as an object carrying `USERTAG` (echo of the
  request's `userTag`) plus a `TICK:` or `OHLC:` array of records.
- Reference/lookup responses wrap a single UPPERCASE plural key, e.g.
  `EXCHANGES`, `INSTRUMENTS`, `INSTRUMENTTYPES`, `PRODUCTS`, `EXPIRYDATES`,
  `OPTIONTYPES`, `STRIKEPRICES`.
- NOTE: the REST samples show `LASTTRADETIME`/`SERVERTIME` in epoch
  **milliseconds** while the WS samples show epoch **seconds** — a live probe
  should confirm which the server actually serves per transport
  (`../VERDICT.md` honest limits).

## Endpoint catalogue
*(source: vendor REST reference captured from PyPI gfdl-rest@1.0.3, summarized; the vendor's numbered function list has 28 entries — 27 API functions plus a closing "Parameters" table section. The shipped SDK exposes matching classes for MOST functions, but the two 27-member sets are NOT identical: `GetHistoryAfterMarket` is reference-documented with NO `gfdl_rest` class (grep: absent from the package), while `GetInstrumentsOnSearch` ships as a class + reference body section but is absent from the numbered list)*

### Quotes + snapshots

| Endpoint | Purpose (own words) | Request parameters | Response fields (names) |
|---|---|---|---|
| `GetLastQuote/` | Latest full L1 record for ONE symbol | `exchange`, `instrumentIdentifier`, optional `isShortIdentifier` | `AVERAGETRADEDPRICE, BUYPRICE, BUYQTY, CLOSE, EXCHANGE, HIGH, INSTRUMENTIDENTIFIER, LASTTRADEPRICE, LASTTRADEQTY, LASTTRADETIME, LOW, OPEN, OPENINTEREST, PREOPEN, QUOTATIONLOT, SELLPRICE, SELLQTY, SERVERTIME, TOTALQTYTRADED, VALUE, PRICECHANGE, PRICECHANGEPERCENTAGE, OPENINTERESTCHANGE` |
| `GetLastQuoteShort/` | Latest quote, trimmed field set | same as above | `BUYPRICE, EXCHANGE, INSTRUMENTIDENTIFIER, LASTTRADEPRICE, LASTTRADETIME, SELLPRICE` |
| `GetLastQuoteShortWithClose/` | Trimmed quote + previous-day close | same | short set + `CLOSE` |
| `GetLastQuoteArray/` | Latest full L1 records for up to 25 symbols (joined with `+`) | `exchange`, `instrumentIdentifiers`, optional `isShortIdentifier` | array of the full GetLastQuote record |
| `GetLastQuoteArrayShort/` | Trimmed multi-symbol quotes (max 25) | same | array of the short record |
| `GetLastQuoteArrayShortWithClose/` | Trimmed multi-symbol quotes + previous close (max 25) | same | array of short + `CLOSE` |
| `GetSnapshot/` | Latest completed bar per symbol at a chosen `Periodicity`/`Period` (max 25 symbols) | `exchange`, `periodicity`, `period`, `instrumentIdentifiers`, optional `isShortIdentifiers` | `OPEN, HIGH, LOW, CLOSE, TRADEDQTY, OPENINTEREST, LASTTRADETIME, EXCHANGE, INSTRUMENTIDENTIFIER` |
| `GetExchangeSnapshot/` | Whole-exchange snapshot at a `Periodicity`/`Period` | `exchange`, `periodicity`, `period`, optional `instrumentType`, `from`, `to`, `nonTraded` | bulk snapshot (vendor sample served as a downloadable file, not inlined) |
| `GetTopGainersLosers/` | Top movers list for an exchange | `exchange`, `count` | movers list |

### History (the BruteX-relevant family)

| Endpoint | Purpose (own words) | Request parameters | Response fields (names) |
|---|---|---|---|
| `GetHistory/` | Historical records at a chosen periodicity — **two request forms**: (a) **span form** with `from`+`to` (Unix epoch SECONDS), returns every record in the span; (b) **latest-N form** with `max`, returns the newest N records | `exchange`, `instrumentIdentifier`, `periodicity`, `period`, then `from`+`to` OR `max`; optional `isShortIdentifier`, `userTag` | `TICK` records: `BUYPRICE, BUYQTY, LASTTRADEPRICE, LASTTRADETIME, OPENINTEREST, QUOTATIONLOT, SELLPRICE, SELLQTY, TRADEDQTY`. `OHLC` records: `CLOSE, HIGH, LASTTRADETIME, LOW, OPEN, OPENINTEREST, QUOTATIONLOT, TRADEDQTY`. Wrapper: `USERTAG` + `TICK:`/`OHLC:` array |
| `GetHistoryAfterMarket/` | Same request shape as `GetHistory`; serves history only up to the previous working day (current day only after market close) — positioned by the vendor for backtesting users to reduce API cost | as `GetHistory` | as `GetHistory` |
| `GetHistoryGreeks/` | Historical option-Greeks series for one instrument | `exchange`, `instrumentIdentifier`, optional `isShortIdentifier`, `max`, `from`, `to` | per-record: `Token, IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna, Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio, DTR, Timestamp` |

**Periodicity enum** (identical across REST + WS): `TICK` / `MINUTE` / `HOUR` /
`DAY` / `WEEK` / `MONTH`, default `TICK`; integer `Period` multiplier ("1, 2, 3…",
default 1) applies to `MINUTE`/`HOUR`/`DAY` only. **No 1-second periodicity
exists.** GDFL's `TICK` = the irregular 1-second L1 snapshot records described in
`../VERDICT.md` — not OHLC, not sub-second exchange tick-by-tick.

### Option chain + Greeks (last-quote family)

| Endpoint | Purpose (own words) | Request parameters |
|---|---|---|
| `GetLastQuoteOptionChain/` | Last-traded prices across an underlying's whole option chain | `exchange`, `product`, optional `expiry`, `optionType`, `strikePrice` |
| `GetLastQuoteOptionGreeks/` | Last-traded Greeks for one option (addressed by numeric `Token`) | `exchange`, `token`, optional `detailedInfo` |
| `GetLastQuoteArrayOptionGreeks/` | Last-traded Greeks for up to 25 tokens (joined with `+`) | `exchange`, `tokens`, optional `detailedInfo` |
| `GetLastQuoteOptionGreeksChain/` | Last-traded Greeks across a whole option chain | `exchange`, `product`, optional `expiry`, `optionType`, `strikePrice` |

Greeks response field names: `EXCHANGE, TOKEN, TIMESTAMP, IV, DELTA, THETA, VEGA,
GAMMA, IVVWAP, VANNA, CHARM, SPEED, ZOMMA, COLOR, VOLGA, VETA, THETAGAMMARATIO,
THETAVEGARATIO, DTR`.

### Instrument master + account/reference

| Endpoint | Purpose (own words) | Request parameters | Response key |
|---|---|---|---|
| `GetExchanges/` | Exchanges enabled for the key | — | `EXCHANGES` (sample tokens: `BFO, BSE, BSE_DEBT, BSE_IDX, CDS, MCX, NFO, NSE, NSE_IDX`) |
| `GetInstruments/` | Instrument list for an exchange, filterable | `exchange`, optional `product`, `instrumentType`, `optionType`, `expiry`, `strikePrice`, `onlyActive`, `detailedInfo` | `INSTRUMENTS` — per row: `EXCHANGE, EXPIRY, IDENTIFIER, INDEXNAME, NAME, OPTIONTYPE, PRICEQUOTATIONUNIT, PRODUCT, PRODUCTMONTH, STRIKEPRICE, TRADESYMBOL, UNDERLYINGASSET, UNDERLYINGASSETEXPIRY, QUOTATIONLOT, DESCRIPTION` |
| `GetInstrumentsOnSearch/` | Search-string instrument lookup (max 20 results) | `exchange`, `search`, + the GetInstruments filters | as `GetInstruments` |
| `GetInstrumentTypes/` | Instrument-type tokens for an exchange | `exchange` | `INSTRUMENTTYPES` (sample: `FUTIDX, FUTSTK, OPTIDX, OPTSTK`) |
| `GetProducts/` | Product (underlying) tokens for an exchange | `exchange`, optional `instrumentType` | `PRODUCTS` |
| `GetExpiryDates/` | Expiry tokens (`DDMONYYYY`) | `exchange`, optional `product`, `instrumentType` | `EXPIRYDATES` |
| `GetOptionTypes/` | Option-type tokens | `exchange`, optional filters | `OPTIONTYPES` (sample: `FF, XX, CE, PE`) |
| `GetStrikePrices/` | Strike tokens | `exchange`, optional filters | `STRIKEPRICES` |
| `GetServerInfo/` | ID of the serving backend | — | `ServerID` |
| `GetLimitation/` | The key's entitlement record (see below) | — | see below |
| `GetMarketMessages/` | Recent market-status messages for an exchange | `exchange` | `MESSAGES` (`SESSIONID, MARKETTYPE, SERVERTIME`) |
| `GetExchangeMessages/` | Recent exchange-issued messages | `exchange` | `MESSAGES` |

## GetLimitation — the entitlement record
*(source: vendor REST reference captured from PyPI gfdl-rest@1.0.3, summarized; the values below are the reference's SAMPLE response — per-account, NOT global constants)*

Field names: `AllowedBandwidthPerHour`, `AllowedCallsPerHour`,
`AllowedCallsPerMonth`, `AllowedBandwidthPerMonth` (`-1` = unlimited in the
sample), `ExpirationDate`, `Enabled`, `AllowedExchanges` (per exchange:
`AllowedInstruments`, `UsedInstrumentsInThisSession`, `DataDelay`,
`ExchangeName`), `AllowedFunctions` (per function: `FunctionName`, `IsEnabled`),
`HistoryLimitation` (`TickEnabled`, `DayEnabled`, `WeekEnabled`, `MonthEnabled`,
`MaxEOD`, `MaxIntraday`, `MaxTicks`, per-`Minute_N`/`Hour_N` enable flags,
`OnlyAfterMarket`), `SubscribeSnapshotLimitation`, `GetSnapshotLimitation`,
`GetExchangeSnapshotLimitation`, `GetExchangeSnapshotInstrumentTypeLimitation`.

Sample values: `MaxEOD: 100000`, `MaxIntraday: 44`, `MaxTicks: 7` — **units NOT
STATED anywhere in the SDK**; the WS reference's sample shows `MaxTicks: 5`, so
these are per-account entitlements. Probe with a real key (`../VERDICT.md` gate 1).

## Instrument identifier conventions
*(source: vendor REST reference captured from PyPI gfdl-rest@1.0.3, summarized)*

- Current-contract shorthand: `NIFTY-I`, `BANKNIFTY-I`, `SBIN-I`, `FINNIFTY-I`.
- Long form: `FUTIDX_NIFTY_28JUL2022_XX_0`, `OPTIDX_NIFTY_22AUG2024_CE_27800`
  (pattern `NAME_PRODUCT_DDMONYYYY_{CE|PE|XX}_STRIKE`).
- Short form (with `isShortIdentifier=true`): `NIFTY25MAR21FUT`,
  `NIFTY25MAR2115000CE` — the exchange-ticker-style identifier.
- Option-Greeks calls address instruments by numeric `Token` (obtained from
  `GetInstruments`/`GetInstrumentsOnSearch`). Max 25 symbols/tokens per array
  call.

## Shipped-SDK defects worth knowing (code-verified, gfdl-rest@1.0.3)

- The `GetHistoryGreeks` class requests the `GetHistory/` path (not a dedicated
  Greeks path) and omits periodicity/period.
- The `GetExchangeSnapshot` class sends its `nonTraded` argument under the query
  name `max`.
- Falsy-value guards silently drop `0`/`False`/empty-string arguments from the
  query string.

These reinforce the wire-protocol-direct recommendation in `../sdk/PACKAGES.md`.

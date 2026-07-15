# 05 — Instrument master, discovery & market-info functions

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Sources: GFDLWS 0.0.4 + gfdl-rest 1.0.3 vendor
READMEs & source (pypi-fetch, high) unless noted. All functions exist on
BOTH WS (MessageType) and REST (URL path); the REST list per the
gfdl-rest README is 27 functions (it omits the WS-only `Subscribe*`
streams and includes GetTopGainersLosers + GetHistoryGreeks).

## 1. Instrument master & discovery

| Function | Params | Returns | Notes |
|---|---|---|---|
| GetExchanges | — | list of exchanges configured **FOR THE API KEY**. WS sample: `CDS, MCX, NFO, NSE, NSE_IDX` (`"MessageType":"ExchangesResult"`). REST sample: `{'EXCHANGES': ['BFO','BSE','BSE_DEBT','BSE_IDX','CDS','MCX','NFO','NSE','NSE_IDX']}` | per-key list, NOT the vendor's full universe (the two vendor samples differ because the keys differ). Full observed exchange-token vocabulary: NSE, NSE_IDX, NFO, CDS, BSE, BSE_IDX, BFO, BSE_DEBT, MCX. NCDEX: claimed at company level, never observed as an API token — UNVERIFIED |
| GetInstruments | Exchange, [InstrumentType, Product, Expiry, OptionType, StrikePrice, OnlyActive] | full instrument array for the exchange | per-record fields below |
| GetInstrumentsOnSearch | Exchange, Search, [same optional filters] | max **20** instruments matching the search string | same record shape |
| GetInstrumentTypes | Exchange | e.g. `FUTIDX, FUTSTK, OPTIDX, OPTSTK` | `{"Value":…}` array |
| GetProducts | Exchange, [InstrumentType] | product/underlying names (e.g. NIFTY, BANKNIFTY, AARTIIND…) | `{"Value":…}` array |
| GetExpiryDates | Exchange, [InstrumentType, Product] | expiries e.g. `21JUL2022, 26JUL2022, …` | `{"Value":…}` array |
| GetOptionTypes | Exchange, [InstrumentType, Product, Expiry] | `CE, PE` | |
| GetStrikePrices | Exchange, [InstrumentType, Product, Expiry, OptionType] | strike list | third-party REST usage in the wild passes **`expiry=0`** apparently meaning "nearest" (github-fetch; numeric-expiry-index semantics UNVERIFIED in vendor docs) |

Instrument record field inventory (WS README sample, summarized; the
identifier examples are facts — grammar in [07](./07-symbology.md)):

| Field | Content |
|---|---|
| `Identifier` | the long GFDL form, e.g. `OPTIDX_NIFTY_21JUL2022_CE_12900` |
| `Name` | instrument type token (e.g. `OPTIDX`) |
| `Expiry` · `ProductMonth` | mixed-case `DDMonYYYY` form (e.g. `21Jul2022`) |
| `StrikePrice` | float (`0` slot for futures) |
| `Product` | underlying name (e.g. `NIFTY`) |
| `OptionType` | `CE` / `PE` (`XX` slot for futures) |
| `TradeSymbol` | exchange trading symbol, e.g. `NIFTY21JUL2212900CE` |
| `QuotationLot` | lot size (float) |
| `TokenNumber` | numeric string — REQUIRED by the single-token Greeks calls ([06](./06-greeks-optionchain.md)) |
| `LowPriceRange` · `HighPriceRange` | day price band |
| `PriceQuotationUnit` · `UnderlyingAsset` · `UnderlyingAssetExpiry` · `IndexName` · `Description` | present, empty strings in the vendor sample |

(The REST record adds `EXCHANGE`; the REST sample omits the token.)

## 2. Whole-exchange snapshot (bulk bars)

**GetExchangeSnapshot** — "entire Exchange Snapshot as per Period &
Periodicity"; params Exchange, Periodicity, Period, [InstrumentType,
From, To, nonTraded]. Response `ExchangeSnapshotResult`; the vendor
sample exists only as the ZIP `GetExchangeSnapshot_1Min_JSON.zip`
(not retrievable here — response shape UNVERIFIED beyond the envelope
name). A dedicated REST doc page exists
(`…/rest-api-documentation/getexchangesnapshot/`). This is the vendor's
bulk "one call = every instrument's current bar" facility — directly
relevant to whole-universe pulls (TickVault capture / BruteX movers-style
sweeps). (pypi-fetch vendor README, high; shape UNVERIFIED.)

## 3. Market/exchange messages & server/account info

| Function | Returns (vendor samples, summarized; high) |
|---|---|
| GetMarketMessages | `MarketMessagesResult` envelope: a `Request` echo (`Exchange` + `MessageType`) + a `Result` array of `MarketMessagesItemResult` items carrying `ServerTime` (epoch s) · `SessionID` · `MarketType` (observed token: `Normal Market Open`) · `Exchange` |
| GetExchangeMessages | array of `ExchangeMessagesItemResult` items: `ServerTime` · `Identifier` (observed token: `Market`) · `Message` (free-text exchange notice) |
| GetServerInfo | `ServerInfoResult` carrying `ServerID` (serving-backend id string) |
| GetLimitation | the per-key entitlement record — field-by-field in [08-limits-entitlements-pricing.md](./08-limits-entitlements-pricing.md) |

`GetMarketMessages` (`MarketType: "Normal Market Open"`) is the closest
thing to a market-status API in the vocabulary — no holiday-calendar
endpoint was observed (UNVERIFIED/absent).

## 4. Top gainers / losers

- `GetTopGainersLosers` (one-shot): `{"MessageType":"GetTopGainersLosers","Exchange":"<exg>","Count":"<n>"}` —
  the vendor lib awaits `LastQuoteArrayResult` for it.
- `SubscribeTopGainersLosers` (stream): same params + `Unsubscribe` →
  streams `RealtimeGainersLosersResult`.
(GFDLWS `topgainerslosers.py`, pypi-fetch, high. The vendor docs file the
stream under "Advanced Streaming API".)

## 5. Fundamental data & news APIs

The authorised-vendors page markets a "Fundamental Data & Stock Market
News API" (search-relay, medium). NO endpoint/function documentation for
these was retrievable — **UNVERIFIED beyond existence-as-marketed**; they
are NOT part of the WS/REST function vocabulary above.

## 6. Corporate actions

No corporate-actions function appears in any retrieved function list —
**appears ABSENT** from the WS+REST vocabulary (high-confidence negative
for the two vendor packages; UNVERIFIED for other product surfaces).
Contrast: TrueData ships `/getcorpaction` (see
`docs/truedata/api-reference/04-historical-rest.md`).

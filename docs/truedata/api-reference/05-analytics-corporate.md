# 05 — Analytics, Greeks, option-chain & corporate-data APIs

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Hosts (vendor SDK source — pkg-source, HIGH)

| Service | Base URL | Auth |
|---|---|---|
| Analytics | `https://analytics.truedata.in/api/` | Bearer token (same auth.truedata.in/token) |
| Greeks | `https://greeks.truedata.in/api/` | Bearer token |
| Utility/symbol API | `https://api.truedata.in/` | `user` + `password` query params |
| Corporate REST | `https://corporate.truedata.in` | Bearer token |
| Corporate WS | `wss://corp.truedata.in:9092?user=&password=` | query params |

## 2. Analytics endpoints (`truedata@7.0.1 analytics/TD_analytics.py` — pkg-source, HIGH)

| SDK method | Endpoint | Params |
|---|---|---|
| get_option_chain(symbol, expiry, greeks=False) | `analytics…/getoptionchain` or (greeks=True) `greeks…/getoptionchainwithgreeks` | `symbol`, `expiry=DD-MM-YYYY`, `comp=true` |
| get_oi_gainer_losers(top, series, sort_by) | `analytics…/get{sort_by}` where sort_by ∈ `futureswithhighestoi, optionswithhighestoi, oigainers, oilosers, oigainerspricegainers, oigainerspricelosers, oiloserspricegainers` (list as coded; one duplicate entry in the vendor list) | `top`, `series` |
| get_index_gainer_losers(top, segment, index_name, sort_by) | `analytics…/getindex{sort_by}` | `top`, `indexname`, `segment` |
| get_industry_gainer_losers(top, segment, industry, sort_by) | `analytics…/getindustry{sort_by}` | `top`, `segment`, `industry` |
| get_history_greeks(symbol, expiry, strike, series, ltp=False) | `greeks…/getTickHistorywithGreeks` or (ltp=True) `greeks…/getltpwithgreeks` | `symbol`, `expiry=DD-MM-YYYY`, `strike`, `series` |
| get_bulk_spot_ltp(symbols) | `analytics…/getLTPSpotbulk` | `symbols` |
| get_spot_ltp(symbol) | `analytics…/getLTPspot` | `symbol` |
| get_fno_ltp(symbol, expiry, strike=0, series='XX') | `analytics…/getLTP` | `symbol`, `expiry`, `strike`, `series` |

Behaviors (pkg-source, high): 429 → base64-encoded error body (SDK
decodes → `TooManyRequestsError`); responses are LZ4-block-compressed
CSV when `comp=true` (same 8-byte header scheme as history) → pandas
DataFrame. Analytics arrived in the SDK line at truedata-ws v5.0.10
("TD Analytics added" — PyPI changelog, direct-fetch, high).

**Historical greeks:** `getTickHistorywithGreeks` implies TICK-level
greeks history exists on the greeks host; its DEPTH is **UNVERIFIED**.

## 3. Utility API on api.truedata.in (pkg-source, HIGH)

| Endpoint | Params (as built) | Purpose |
|---|---|---|
| `GET /getAllSymbols` | `segment=all&user=&password=&search=<sym>` or `…&allexpiry=false` | Symbol master / id lookup ([06](./06-symbology.md)) |
| `GET /getATMStrike` | `user, password, symbol, expiry=YYYYMMDD` → `{status:'Success', Records:{strike, strikestep}}` | ATM strike + strike step for chain construction |
| `GET /getOptionChain` | user/password/symbol/expiry (per Python `TD_chain`) | Option-chain snapshot bootstrap |
| `GET /getprevcloseandoi` | `segment=<EQ|FO|MCX|CDS|IN|BSEEQ|BSEFO>&user=&password=` | Prev close + OI + symbol-id master per segment (full-feed cache) |

## 4. Live option chain (SDK feature composed over WS + utility API)

- Python: `td_obj.start_option_chain(symbol, expiry: datetime,
  chain_length=10, bid_ask=False, greek=False)` → OptionChain object;
  `.get_option_chain()` returns a DataFrame. Chain symbols are GENERATED
  as `{SYMBOL}{YYMMDD}{STRIKE}{CE|PE}` around ATM using the strike-step
  from getATMStrike; updates merge from the tick + greeks streams.
  (pkg-source + PyPI README, high.)
- Node: `startOptionChain(symbol, expiry='YYYYMMDD', chainLength=10,
  isBidAsk, isGreeks)` — same scheme; emits `optionChain` events. (high)
- BSE variant (older SDK): `start_option_chain('SENSEX', dt(...),
  chain_length=80, bse_option=True)` (PyPI truedata-ws README,
  direct-fetch, high).
- Greeks merged per contract: IV, Delta, Theta, Gamma, Vega, Rho. (high)
- KB article "How to fetch Options Chain using TrueData Python Library"
  exists (search-relay; content 403-blocked).

## 5. Corporate-data APIs (Node SDK `corporate.js` + `utils.js` corpRoutes — pkg-source, HIGH)

REST base `https://corporate.truedata.in`; the full route list:

```
/getdescriptors                     /announcements (getAnnouncementsList)
/getannouncementsforcompanies       /getannouncementsforcompaniesfrom
/getannouncementsforcompaniesrange  /getAllAnnouncementByIds
/announcementfile                   /getAllResultsByCompany
/getAllResultItemsById              /getPnLById
/getBalSheetById                    /getCashFlowDetailById
/getCashFlowSummaryById             /getSHPListByDate
/getSHPListByCompany                /getSHPDetailById
/getSHPSummaryById                  /getAllSHPById
/getSHPMemberTypes                  /getfiidiidata
/getCorporateInfo                   /getCorporateAbout
/getSymbolClassification            /getSymbolClassificationBulk
/getresultcalendar                  /getPeerCompanies
/getMarketCap
```

- Coverage: corporate announcements (+ live push over
  `wss://corp.truedata.in:9092`), quarterly/yearly results (P&L, balance
  sheet, cash flow; standalone/consolidated), shareholding patterns
  (SHP), FII/DII data (`segment='fo'` param seen), market caps, peer
  companies, symbol classification, result calendar (npm README,
  direct-fetch, high).
- Params of note (npm README): `nature ∈ standalone|consolidated`,
  `period ∈ yearly|quarterly`, `type ∈ pl|cf|bs`, `duringmarket 0|1`,
  date format `'251231T09:00:00'` or a JS Date.
- Timeline: npm 1.1.0 (2026-01-14) / 1.2.0 (2026-05-08) added/extended
  this corporate + option-chain surface (registry JSON, direct-fetch,
  high).

## 6. Other analytics-ish surfaces

- Python live/hist modules also expose `get_bhavcopy(segment)`,
  `get_gainers(segment, topn)`, `get_losers(segment, topn)` (PyPI,
  direct-fetch, high).
- .NET SDK exposes `GetCorporateActions()`, `Get52WeekHighLow()`,
  `GetTopGainers/Losers`, `GetBhavCopy(Status)` (NuGet, direct-fetch,
  high).
- "TrueData Options Decoder" (with Option Chain Replay + OI graphs) is a
  separate end-user PRODUCT, not an API — see [09](./09-ops-misc.md) §6;
  API-level exposure UNVERIFIED.

## 7. Notes for BruteX / TickVault

- TrueData's greeks (stream + chain + history) are VENDOR-COMPUTED;
  BruteX computes its own stamped Greeks at enrich time
  (`brutex/data/greeks_enrich.py`) — vendor greeks would be
  REFERENCE_ONLY at best (formula/inputs UNVERIFIED).
- The corporate surface (results, SHP, FII/DII, announcements) has no
  BruteX Track-1 use today but is recorded for completeness — it is the
  widest ancillary-data surface in this two-vendor study.

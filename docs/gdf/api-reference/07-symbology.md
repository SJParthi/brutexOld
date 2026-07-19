# 07 — Symbology ("GFDL symbols")

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor pages: `…/documentation-support/symbol-naming-conventions/`
and the site KB `/articles/symbol-naming-conventions/` (both observed;
neither fetchable directly — evidence is vendor-package examples
(pypi-fetch, high) + search-relay of the symbology article (medium)).

## 1. Exchange tokens (the `Exchange` request parameter)

| Token | Meaning | Evidence | Confidence |
|---|---|---|---|
| NSE | NSE cash equities | vendor REST samples `Get_LastQuote('NSE','SBIN')` / `('NSE','SBIN-I')` (pypi-fetch) | high |
| NSE_IDX | NSE indices | GetExchanges samples (pypi-fetch) | high |
| NFO | NSE F&O | vendor examples throughout | high |
| CDS | NSE currency derivatives | GetExchanges samples | high |
| MCX | MCX commodities | GetExchanges samples + third-party `exchange=MCX&instrumentIdentifiers=SILVER-I+GOLD-I` | high/medium |
| BSE / BSE_IDX / BFO / BSE_DEBT | BSE cash / BSE indices / BSE F&O / BSE debt | REST GetExchanges sample (pypi-fetch) | high |
| NCDEX | claimed at company level ("Authorized … NSE, BSE, MCX & NCDEX"); never observed as an API token | search-relay (claim) | UNVERIFIED as API token |

## 2. Instrument identifier formats

### 2.1 Long/full identifier (the canonical `Identifier` in the instrument master)

Pattern (observed): `<INSTRTYPE>_<PRODUCT>_<DDMONYYYY>_<OPTIONTYPE|XX>_<STRIKE|0>`

Verbatim vendor examples (pypi-fetch, high):
- Futures (option-type slot `XX`, strike slot `0`):
  `FUTIDX_NIFTY_28JUL2022_XX_0` · `FUTIDX_BANKNIFTY_28JUL2022_XX_0` ·
  `FUTIDX_FINNIFTY_27AUG2024_XX_0`
- Options: `OPTIDX_NIFTY_21JUL2022_CE_12900` ·
  `OPTIDX_NIFTY_22AUG2024_CE_27800` · `OPTSTK_EXIDEIND_29SEP2022_PE_116`
- The vendor symbology article shows the same grammar
  (`FUTIDX_NIFTY_30APR2020_XX_0`, `OPTIDX_NIFTY_07MAY2020_PE_9500`) —
  search-relay, medium.

Instrument types observed: `FUTIDX, FUTSTK, OPTIDX, OPTSTK` (NFO).
Commodity/currency instrument-type tokens (FUTCOM/OPTFUT/FUTCUR…):
**UNVERIFIED** (not in retrieved samples).

### 2.2 Continuous-futures aliases

- `NIFTY-I` = current/near-month future; `NIFTY-II` = next month;
  `NIFTY-III` = far month; same per stock (`RELIANCE-I`, `SBIN-I`) and
  MCX (`SILVER-I`, `GOLD-I`).
- Evidence: vendor READMEs use `NIFTY-I` / `BANKNIFTY-I` / `FINNIFTY-I`
  throughout (pypi-fetch, high); the I/II/III scheme + "NIFTY-I always
  represents current month Futures" via search-relay of the symbology
  article (medium); MCX `-I` via third-party code (github-fetch, low).
- Aliases resolve SERVER-SIDE to the dated contract: a `GetSnapshot` for
  `NIFTY-I` answers with `FUTIDX_NIFTY_28JUL2022_XX_0` (vendor sample,
  high). Roll timing convention (expiry-day ownership): **UNVERIFIED** —
  BruteX's own futures-series law (`brutex/data/futures_series.py`,
  expiry day owned by the expiring contract) must NOT be assumed to match.
- Oddity: a vendor REST example applies `-I` on the NSE CASH exchange
  (`Get_LastQuote('NSE','SBIN-I')` answering `INSTRUMENTIDENTIFIER 'SBIN'`)
  — the suffix appears tolerated/normalized on cash; plain `SBIN` also
  works (both in the vendor README, high; the cash `-I` semantics are an
  inference, medium).

### 2.3 Short identifiers (`isShortIdentifier=true`)

Vendor parameter-table examples (verbatim, high): `NIFTY25MAR21FUT`,
`RELIANCE25MAR21FUT`, `NIFTY25MAR2115000CE` — i.e.
`<PRODUCT><DDMONYY><FUT | <STRIKE><CE/PE>>`.

### 2.4 TradeSymbol (exchange trading symbol)

Carried per instrument record, distinct from the GFDL Identifier:
`NIFTY21JUL2212900CE`, `NIFTY22AUG2427800CE`, `EXIDEIND29SEP22116PE`
(vendor samples, high).

### 2.5 Indices

- The vendor symbology article writes NSE indices as `NIFTY 50.NSE_IDX`,
  `NIFTY BANK.NSE_IDX` (charting-platform suffix style). On the API the
  exchange rides the separate `Exchange=NSE_IDX` parameter, so the
  identifier is presumably `NIFTY 50` / `NIFTY BANK` with a space —
  search-relay + inference, medium.
- INDIA VIX: NSE-index coverage is claimed as a segment, but the exact
  GFDL identifier for India VIX is **UNVERIFIED** — do NOT hardcode;
  probe `GetInstruments('NSE_IDX')` live.
- Name-space split: the INDEX name is `NIFTY BANK` (relay, medium) while
  the DERIVATIVES product is `BANKNIFTY` (high) — two different name
  spaces; any adapter needs an explicit mapping (BruteX's Groww-side
  equivalent: index alias handling in `brutex/universe.py`).

### 2.6 Array joins

- WS array calls: JSON array of objects
  `[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]` (vendor, high).
- REST array calls: `+`-joined list
  `instrumentIdentifiers=NIFTY-I+BANKNIFTY-I+FINNIFTY-I` (vendor
  gfdl-rest README + third-party usage, high).
- Both capped at **25** instruments per call (vendor READMEs, high).

### 2.7 Token numbers

Numeric `TokenNumber` per instrument (e.g. `"69695"`); REQUIRED for the
single-token Greeks calls (SubscribeRealtimeGreeks /
GetLastQuoteOptionGreeks). Obtained via GetInstruments /
GetInstrumentsOnSearch. (Vendor, high.)

## 3. Expiry-token casing trap (parse defensively)

THREE casings/lengths of the SAME expiry concept coexist (all vendor
samples, high):

| Surface | Form | Example |
|---|---|---|
| Long Identifier | `DDMONYYYY` upper | `21JUL2022` |
| `Expiry` field / `ProductMonth` | `DDMonYYYY` mixed | `21Jul2022` |
| Short ids / TradeSymbol | `DDMONYY` / `YYMMM…` variants | `25MAR21`, `21JUL22` |

## 4. Mapping notes for BruteX

- GFDL `Product` (`NIFTY`, `BANKNIFTY`) ≈ Groww `underlying_symbol`;
  the GFDL long Identifier is the closest analogue of Groww's
  `groww_symbol` contract form but with a different grammar
  (`OPTIDX_NIFTY_21JUL2022_CE_12900` vs `NSE-NIFTY-21Jul22-12900-CE`
  style) — a bidirectional mapper would be needed at any adapter boundary.
- No `&`-encoding lore was observed for GFDL identifiers (M&M-class
  names): **UNVERIFIED** how `&`-carrying products are spelled — probe
  before assuming either raw `&` or an encoding (the Groww `%26`
  hypothesis history in CLAUDE.md §6 is a caution tale).

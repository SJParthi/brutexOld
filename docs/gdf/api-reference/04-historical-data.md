# 04 — Historical data (GetHistory family, depth, timestamps, caps)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **REST API →
Function GetHistory / GetHistoryAfterMarket**, **Greeks API →
GetHistoryGreeks**, **Introduction → Type of Data Available** (depth).
History is served over BOTH transports — WS MessageTypes and REST paths
with the same function names.

## 1. Functions

| Function | What it returns | Channel | Source | Confidence |
|---|---|---|---|---|
| GetHistory | Historical TICK / MINUTE / HOUR / DAY / WEEK / MONTH rows, either by From/To window or by `Max` latest records | WS + REST | GFDLWS README + gfdl-rest README (pypi-fetch) | high |
| GetHistoryAfterMarket | Same as GetHistory but only up to the previous working day (current day's data available only after market close). Vendor: "useful for … back-testing … This should also save their API costs." | WS + REST | GFDLWS README (pypi-fetch) | high |
| GetHistoryGreeks | Historical option-Greeks rows → `HistoryGreeksResult`; params Exchange, InstrumentIdentifier, isShortIdentifier, Max, From, To | WS (+ REST doc page exists) | GFDLWS `historygreeks.py` (pypi-fetch); search-relay for the REST page | high (WS) / medium (REST) |

## 2. WS wire requests — verbatim (GFDLWS `history.py`, high)

By window:
```json
{"MessageType":"GetHistory","Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","Periodicity":"MINUTE","Period":1,"From":"1658115000","To":"1658138400","isShortIdentifier":"false","UserTag":"dhananjay"}
```
By latest-N records:
```json
{"MessageType":"GetHistory","Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","Periodicity":"MINUTE","Period":1,"Max":10,"isShortIdentifier":"false"}
```
Response MessageTypes: `HistoryOHLCResult` (MINUTE/HOUR/DAY/WEEK/MONTH)
or `HistoryTickResult` (TICK).

WS response row shapes (README verbatim; epoch SECONDS):
```json
// OHLC row
{"LastTradeTime":1658138400,"QuotationLot":50,"TradedQty":800,"OpenInterest":11179850,"Open":16312.95,"High":16312.95,"Low":16312.95,"Close":16312.95}
// TICK row
{"LastTradeTime":1594186301,"LastTradePrice":10775.0,"QuotationLot":75,"TradedQty":225,"OpenInterest":12176625,"BuyPrice":10774.3,"BuyQty":75,"SellPrice":10775.0,"SellQty":3150}
```

## 3. REST request — verbatim construction (gfdl-rest `GetHistory.py`, high)

```
GET {base}/GetHistory/?accessKey=…&exchange=…&instrumentIdentifier=…&periodicity=…&period=…[&max=…][&from=…][&to=…][&userTag=…][&isShortIdentifier=…]
```
(+ `xml=true` | `format=CSV` output selectors.)

REST response envelope (gfdl-rest README verbatim — note **milliseconds**
epoch and ALL-CAPS field names):
```json
{"USERTAG": "surendran", "OHLC": [{"CLOSE": 24757.3, "HIGH": 24794, "LASTTRADETIME": 1724243400000, "LOW": 24685.05, "OPEN": 24697.7, "OPENINTEREST": 10906875, "QUOTATIONLOT": 25, "TRADEDQTY": 2223325}, …]}
{"USERTAG": "surendran", "TICK": [{"BUYPRICE": 24753.8, "BUYQTY": 50, "LASTTRADEPRICE": 24753.15, "LASTTRADETIME": 1724231023000, "OPENINTEREST": 10906875, "QUOTATIONLOT": 25, "SELLPRICE": 24758, "SELLQTY": 25, "TRADEDQTY": 625}, …]}
```

## 4. Timestamp semantics — TWO conventions (adapter-critical)

| Claim | Value | Source | Confidence |
|---|---|---|---|
| Request From/To | Unix epoch **seconds** ("no. of seconds since Epoch … 1st January 1970"), both WS and REST | both vendor READMEs (pypi-fetch) | high |
| WS responses | epoch **seconds** (`LastTradeTime: 1669262572`) | GFDLWS README samples | high |
| REST responses | epoch **milliseconds** (`LASTTRADETIME: 1724243400000`) + ALL-CAPS field names | gfdl-rest README samples | high |
| Wall-clock mapping | Vendor's own example: `1658138400` = "18-07-2022 15:30:00" — that epoch is 2022-07-18T10:00:00Z = 15:30 IST ⇒ genuine Unix time. Daily rows appear stamped at 15:20/15:30 IST session times (e.g. `1724243400000` = 2024-08-21 15:20 IST) | vendor READMEs + arithmetic | high (Unix-UTC epoch) / medium (exact daily-bar stamp convention) |

## 5. Periodicities & per-key enablement

Periodicity ∈ TICK / MINUTE / HOUR / DAY / WEEK / MONTH with integer
`Period` multipliers (vendor README, high). The GetLimitation response
(see [08](./08-limits-entitlements-pricing.md)) carries per-key enable
flags for Minute_1/2/3/4/5/6/10/12/15/20/30, Hour_1/2/3/4/6/8/12,
Tick/Day/Week/Month, plus `MaxTicks` / `MaxEOD` / `MaxIntraday` record
caps — i.e. **which intervals and how many records you can pull are
plan/key-configured** (vendor README GetLimitation sample, high).

## 6. Historical DEPTH — how far back

### A) Bundled with realtime API plans (vendor "Type of Data Available" page)

| Periodicity | Depth |
|---|---|
| Tick | 7 days |
| 1–2–3–4 minute | 3 months |
| 5–10 minute | 4.5 months |
| 15–20–30 minute | 6 months |
| 1–2–4–6 hour | 6 months |
| Daily / Weekly / Monthly | since 2010 |

Source: search-relay of `…/introduction/type-of-data-available/`,
2026-07-15. Confidence: **medium** (single relay of a vendor page;
consistent with the GetLimitation per-key caps design). Whether these
exact windows are current for every segment: NOT verified.

### B) Purchasable deep history (IEOD file packs — desktop/data-shop product line, NOT the API)

| Claim | Value | Source | Confidence |
|---|---|---|---|
| NSE F&O 1-min history | since **2010-09-01**, "backadjusted Futures Format" | marketcalls.in/premium/globaldatafeeds/ieod-pricing via search-relay | low (third-party) |
| MCX 1-min history | since 2013-04-01, contract-wise futures format | same | low |
| NSE Currency 1-min | since 2014-04-07, contract-wise | same | low |
| Realtime desktop DB carries | ~2 months of 1-min intraday; longer history = separate purchase/import | same | low |
| Subscribed-segment 1-min EOD CSV emailed free after market hours (desktop products) | yes | same | low |
| Vendor KB on importing IEOD 1-min into AmiBroker | `/articles/nfo-ieod-data-import-in-amibroker/` exists | search-relay (URL observed) | medium (existence) |

**Honest note for BruteX (load-bearing):** the API-bundled 1-minute depth
(~3 months) is FAR shallower than Groww's probe-verified 2020-01-01
1-minute floor (CLAUDE.md §6). Deep 1-min from GDF appears to be a
separately purchased IEOD FILE dataset, not an API pull. Whether deep
1-min history can be enabled ON the API per key (the `MaxIntraday` /
interval-enable knobs make it structurally possible) is **UNVERIFIED** —
a sales conversation or a live `GetLimitation` + `GetHistory` probe is
the only way to answer it.

### C) Expired / continuous contracts

| Claim | Value | Source | Confidence |
|---|---|---|---|
| Continuous futures aliases (`NIFTY-I/-II/-III`) queryable in history without expiry bookkeeping | yes — vendor GetHistory examples use `NIFTY-I` | GFDLWS/gfdl-rest READMEs (pypi-fetch) | high |
| History for EXPIRED specific option/future contracts via API | **UNVERIFIED** — no retrieved doc states whether expired identifiers remain queryable, nor for how long | — | UNVERIFIED |
| Back-adjusted continuous futures (deep history) | offered in the purchased IEOD dataset ("backadjusted Futures Format") | marketcalls relay | low |
| Roll convention of `-I` on expiry day | **UNVERIFIED** | — | UNVERIFIED |

## 7. Per-request caps

| Claim | Value | Source | Confidence |
|---|---|---|---|
| `Max` request param limits returned records | yes | vendor READMEs | high |
| Per-key record caps `MaxTicks` / `MaxEOD` / `MaxIntraday` | present in GetLimitation (sample values 5 / 100000 / 44 in the vendor README's ILLUSTRATIVE response — not plan quotes) | GFDLWS README | high (field names) |
| Hard server-side per-call row cap (N candles/call) | **UNVERIFIED** (not stated in retrieved material) | — | UNVERIFIED |
| Corporate-action adjustment policy for EOD/IEOD history | **UNVERIFIED** (beyond the IEOD "backadjusted futures" wording) | — | UNVERIFIED |

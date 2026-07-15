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
| GetHistoryAfterMarket | Same as GetHistory but only up to the previous working day (current day's data available only after market close). The vendor positions it for backtesting workloads and as a way to reduce API costs (paraphrased) | WS + REST | GFDLWS README (pypi-fetch) | high |
| GetHistoryGreeks | Historical option-Greeks rows → `HistoryGreeksResult`; params Exchange, InstrumentIdentifier, isShortIdentifier, Max, From, To | WS (+ REST doc page exists) | GFDLWS `historygreeks.py` (pypi-fetch); search-relay for the REST page | high (WS) / medium (REST) |

## 2. WS wire requests — key shapes (from GFDLWS `history.py`, high)

(Identifiers/enum tokens verbatim as facts; sample values replaced by
placeholders per the licensing posture, [README.md](./README.md).)

By window:
```json
{"MessageType":"GetHistory","Exchange":"<exchange>","InstrumentIdentifier":"<identifier>","Periodicity":"MINUTE","Period":1,"From":"<epoch-seconds>","To":"<epoch-seconds>","isShortIdentifier":"false","UserTag":"<tag>"}
```
By latest-N records:
```json
{"MessageType":"GetHistory","Exchange":"<exchange>","InstrumentIdentifier":"<identifier>","Periodicity":"MINUTE","Period":1,"Max":10,"isShortIdentifier":"false"}
```
Response MessageTypes: `HistoryOHLCResult` (MINUTE/HOUR/DAY/WEEK/MONTH)
or `HistoryTickResult` (TICK).

WS response row field inventories (vendor README samples, summarized;
timestamps epoch SECONDS):

| Row kind | Fields |
|---|---|
| OHLC row | `LastTradeTime` · `QuotationLot` · `TradedQty` · `OpenInterest` · `Open` · `High` · `Low` · `Close` |
| TICK row | `LastTradeTime` · `LastTradePrice` · `QuotationLot` · `TradedQty` · `OpenInterest` · `BuyPrice` · `BuyQty` · `SellPrice` · `SellQty` — a 1-second L1 snapshot record, NOT a raw exchange tick |

## 3. REST request — construction (gfdl-rest `GetHistory.py`, high)

```
GET {base}/GetHistory/?accessKey=…&exchange=…&instrumentIdentifier=…&periodicity=…&period=…[&max=…][&from=…][&to=…][&userTag=…][&isShortIdentifier=…]
```
(+ `xml=true` | `format=CSV` output selectors.)

REST response envelope (gfdl-rest README samples, summarized — note
**milliseconds** epoch and ALL-CAPS field names): the top level carries a
`USERTAG` echo plus ONE array keyed `OHLC` or `TICK` by periodicity.
Row field inventories:

| Array | Fields |
|---|---|
| `OHLC` rows | `CLOSE` · `HIGH` · `LASTTRADETIME` · `LOW` · `OPEN` · `OPENINTEREST` · `QUOTATIONLOT` · `TRADEDQTY` |
| `TICK` rows | `BUYPRICE` · `BUYQTY` · `LASTTRADEPRICE` · `LASTTRADETIME` · `OPENINTEREST` · `QUOTATIONLOT` · `SELLPRICE` · `SELLQTY` · `TRADEDQTY` |

`LASTTRADETIME` values in the vendor samples are 13-digit epoch
**milliseconds** (vs the WS samples' 10-digit seconds) — the adapter-facing
divergence pinned in §4.

## 4. Timestamp semantics — TWO conventions (adapter-critical)

| Claim | Value | Source | Confidence |
|---|---|---|---|
| Request From/To | Unix epoch **seconds** (documented as seconds since the Unix epoch, 1970-01-01 — paraphrased), both WS and REST | both vendor READMEs (pypi-fetch) | high |
| WS responses | epoch **seconds** (10-digit `LastTradeTime` values in the vendor samples) | GFDLWS README samples | high |
| REST responses | epoch **milliseconds** (13-digit `LASTTRADETIME` values) + ALL-CAPS field names | gfdl-rest README samples | high |
| Wall-clock mapping | The vendor reference pairs epoch `1658138400` with the wall-clock 15:30 on 2022-07-18 — that epoch is 2022-07-18T10:00:00Z = 15:30 IST ⇒ genuine Unix time. Daily rows appear stamped at 15:20/15:30 IST session times (e.g. `1724243400000` = 2024-08-21 15:20 IST) | vendor READMEs + arithmetic | high (Unix-UTC epoch) / medium (exact daily-bar stamp convention) |

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
| Per-key record caps `MaxTicks` / `MaxEOD` / `MaxIntraday` | present in GetLimitation (the vendor README's ILLUSTRATIVE response shows key-specific caps per record kind — not plan quotes) | GFDLWS README | high (field names) |
| Hard server-side per-call row cap (N candles/call) | **UNVERIFIED** (not stated in retrieved material) | — | UNVERIFIED |
| Corporate-action adjustment policy for EOD/IEOD history | **UNVERIFIED** (beyond the IEOD "backadjusted futures" wording) | — | UNVERIFIED |

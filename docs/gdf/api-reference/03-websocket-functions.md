# 03 — WebSockets API: function vocabulary, streams, quotes, wire shapes

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor section covered: **WebSockets API
documentation** (per-function pages). Primary evidence: **GFDLWS 0.0.4**
(vendor-authored PyPI sdist, uploaded 2024-09-16; predecessor `ws-gfdl`
1.1.0, 2022-08-12, near-identical README) — pypi-fetch = high confidence.

## 1. The function vocabulary

Vendor README "List of Functions" (function names are identifiers — kept
verbatim, in the vendor README's order; high):

1. SubscribeRealtime 2. SubscribeSnapshot 3. GetLastQuote
4. GetLastQuoteShort 5. GetLastQuoteShortWithClose 6. GetLastQuoteArray
7. GetLastQuoteArrayShort 8. GetLastQuoteArrayShortWithClose
9. GetSnapshot 10. GetHistory 11. GetHistoryAfterMarket 12. GetExchanges
13. GetInstrumentsOnSearch 14. GetInstruments 15. GetInstrumentTypes
16. GetProducts 17. GetExpiryDates 18. GetOptionTypes 19. GetStrikePrices
20. GetServerInfo 21. GetLimitation 22. GetMarketMessages
23. GetExchangeMessages 24. GetLastQuoteOptionChain
25. GetExchangeSnapshot 26. SubscribeRealtimeGreeks
27. GetLastQuoteOptionGreeks 28. GetLastQuoteArrayOptionGreeks
29. GetLastQuoteOptionGreeksChain

The 0.0.4 package ADDITIONALLY ships modules not yet in that README list
(high, from `GFDLWS/__init__.py` + module sources): **SubscribeOptionChain**,
**SubscribeOptionChainGreeks**, **SubscribeTopGainersLosers**,
**GetTopGainersLosers**, **GetHistoryGreeks**. The vendor docs place the
chain streams under "Advanced Streaming API" and the Greeks calls under
"Greeks API" (URL evidence, search-relay) — both covered in
[06-greeks-optionchain.md](./06-greeks-optionchain.md); history calls in
[04-historical-data.md](./04-historical-data.md); reference calls in
[05-instruments-reference.md](./05-instruments-reference.md).

**CURRENT-package widening (sibling census reconcile):** the NEWEST vendor
WS package, `wsgfdl-py` 1.3.5 (2025-11-28 — see
[09-sdks-code-samples.md](./09-sdks-code-samples.md)), widens the
vocabulary to **38 request / 36 result MessageTypes** — adding e.g.
`GetVolumeShockers`, `SubscribeSnapshotGreeks`, `GetSnapshotGreeks` and an
`AdjustSplits` split-adjusted-history flag. The full AST-verified
inventory lives in the sibling archive:
[docs/vendors/gdfl/api/WEBSOCKET_API.md](../../vendors/gdfl/api/WEBSOCKET_API.md)
(pypi-fetch, high). This file documents the 29+5 set of GFDLWS 0.0.4, which
remains a subset of 1.3.5's vocabulary.

## 2. Streaming subscriptions — wire shapes (vendor source, summarized; high)

Request/response FIELD NAMES + enum tokens below are vendor identifiers
(kept verbatim as facts); the vendor README's response sample dumps are
recast as field inventories per the licensing posture
([README.md](./README.md)).

### SubscribeRealtime — 1-second push per instrument (runs until market close or unsubscribe)

Request — key shape built by GFDLWS `realtime.py`:
```json
{"MessageType":"SubscribeRealtime","Exchange":"<exchange>","Unsubscribe":"false","InstrumentIdentifier":"<identifier>"}
```
Unsubscribe = the same message with `"Unsubscribe":"true"`.

Response stream — one `MessageType: "RealtimeResult"` frame ≈ every
second, carrying (field inventory from the vendor README sample):
`Exchange` · `InstrumentIdentifier` · `LastTradeTime` + `ServerTime`
(Unix epoch **seconds**) · `AverageTradedPrice` · `BuyPrice`/`BuyQty` +
`SellPrice`/`SellQty` (L1 top-of-book, one level each side) · `Open` /
`High` / `Low` / `Close` (Close = previous close; the change fields key
off it) · `LastTradePrice` / `LastTradeQty` · `OpenInterest` ·
`QuotationLot` · `TotalQtyTraded` · `Value` (session turnover) · `PreOpen`
(bool) · `PriceChange` / `PriceChangePercentage` · `OpenInterestChange`.

Notes (high unless stated): timestamps are Unix epoch **seconds**;
top-of-book only (single Buy/Sell price+qty) — no depth levels in this
message. L2/market-depth stream: **UNVERIFIED** (no retrieved doc shows
one; the `Depth` parameter of SubscribeOptionChain is a chain-width
selector, not book depth). Update cadence: 1-second snapshots — GDF
"tick" data is second-stamped, not raw exchange tick-by-tick (inference
from vendor materials, medium — flagged).

### SubscribeSnapshot — streamed bar snapshots per Periodicity/Period (e.g. 1-min bars as they complete)

Request — key shape built by `realsnapshot.py`:
```json
{"MessageType":"SubscribeSnapshot","Exchange":"<exchange>","Periodicity":"MINUTE","Period":1,"InstrumentIdentifier":"<identifier>"}
```
Response stream — `MessageType: "RealtimeSnapshotResult"` frames per
completed bucket, carrying (vendor README sample, summarized): `Exchange` ·
`InstrumentIdentifier` · `Periodicity` · `Period` · `LastTradeTime`
(epoch seconds) · `TradedQty` · `OpenInterest` · `Open`/`High`/`Low`/`Close`.

## 3. On-demand quote calls (single round-trip over the same WS)

### GetLastQuote
Params: Exchange, InstrumentIdentifier, optional isShortIdentifier
[true/false, default false]. Response: one `MessageType: "LastQuoteResult"`
frame whose field set is IDENTICAL to the `RealtimeResult` inventory in
§2 (same 22 business fields, vendor README sample, summarized).

### GetLastQuoteShort → `LastQuoteShortResult`
Trimmed field set (vendor README sample, summarized): `Exchange` ·
`InstrumentIdentifier` · `LastTradeTime` · `BuyPrice` · `LastTradePrice` ·
`SellPrice`.

### GetLastQuoteShortWithClose → `LastQuoteShortWithCloseResult`
Same as Short + previous-day `Close`. (high)

### Array variants — GetLastQuoteArray / …Short / …ShortWithClose
- Max **25 symbols per call**; identifiers passed as a JSON array of
  `{"Value": …}` objects, e.g. `'[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]'`.
- Envelopes: `LastQuoteArrayResult` / `LastQuoteArrayShortResult` /
  `LastQuoteArrayShortWithCloseResult` wrapping per-symbol records. (high)

### GetSnapshot — latest bar per Periodicity/Period, up to 25 symbols
Response `SnapshotResult`; per-symbol records carry (vendor README sample,
summarized): `InstrumentIdentifier` · `Exchange` · `LastTradeTime` ·
`TradedQty` · `OpenInterest` · `Open`/`High`/`Low`/`Close` · `TokenNumber`
(nullable). NOTE: the response echoes FULL identifiers
(`FUTIDX_NIFTY_28JUL2022_XX_0`) even when the request used continuous
aliases (`NIFTY-I`) — identifier grammar in [07-symbology.md](./07-symbology.md).

## 4. Common parameter table (consolidated from the vendor README, own words; high)

| Parameter | Meaning |
|---|---|
| Exchange | name of supported exchange (see [07-symbology.md](./07-symbology.md)) |
| InstrumentIdentifier(s) | symbol(s); array calls max 25 |
| Unsubscribe | [true]/[false], default false |
| Periodicity | "TICK" / "MINUTE" / "HOUR" / "DAY" / "WEEK" / "MONTH", default "TICK" |
| Period | numeric 1,2,3…, default 1 (applies to MINUTE/HOUR/DAY) |
| From / To | Unix timestamps (seconds since epoch) |
| isShortIdentifier(s) | short-format symbols like `NIFTY25MAR21FUT`, `NIFTY25MAR2115000CE` |
| MAX | record-count limit |
| Product / Expiry / OptionType / StrikePrice | chain selectors (values via the respective Get* functions) |
| Token / Tokens | instrument token numbers (via GetInstruments / GetInstrumentsOnSearch) |
| userTag | echo string returned with the response |
| onlyActive | [true]/[false], default true |
| nonTraded | include non-traded instruments, default "false" |

## 5. Message-type index (request → response), consolidated from vendor source (high)

| Request MessageType | Response MessageType(s) |
|---|---|
| Authenticate | AuthenticateResult |
| SubscribeRealtime | RealtimeResult (stream) |
| SubscribeSnapshot | RealtimeSnapshotResult (stream) |
| GetLastQuote / …Short / …ShortWithClose | LastQuoteResult / LastQuoteShortResult / LastQuoteShortWithCloseResult |
| GetLastQuoteArray / …Short / …ShortWithClose | LastQuoteArrayResult / LastQuoteArrayShortResult / LastQuoteArrayShortWithCloseResult |
| GetSnapshot | SnapshotResult |
| GetHistory / GetHistoryAfterMarket | HistoryOHLCResult · HistoryTickResult |
| GetHistoryGreeks | HistoryGreeksResult |
| GetExchanges | ExchangesResult |
| GetInstruments / GetInstrumentsOnSearch | instrument arrays (envelope name not captured verbatim — vendor README shows raw records) |
| GetInstrumentTypes / GetProducts / GetExpiryDates / GetOptionTypes / GetStrikePrices | value arrays `{"Value":…}` (envelope names not captured verbatim) |
| GetServerInfo | ServerInfoResult |
| GetLimitation | LimitationResult |
| GetMarketMessages | MarketMessagesResult (items: MarketMessagesItemResult) |
| GetExchangeMessages | ExchangeMessagesItemResult (array) |
| GetLastQuoteOptionChain | LastQuoteArrayResult (per GFDLWS `lastquoteoptionchain.py` filter) |
| GetExchangeSnapshot | ExchangeSnapshotResult |
| SubscribeRealtimeGreeks | SubscribeRealtimeGreeks (echoed name, stream) |
| GetLastQuoteOptionGreeks / …Array | LastQuoteOptionGreeksResult / LastQuoteArrayOptionGreeksResult |
| GetLastQuoteOptionGreeksChain | chain envelope (sample only via vendor ZIP — not retrievable) |
| SubscribeOptionChain | RealtimeOptionChainResult (stream) |
| SubscribeOptionChainGreeks | RealtimeOptionChainGreeksResult (stream) |
| SubscribeTopGainersLosers | RealtimeGainersLosersResult (stream) |
| GetTopGainersLosers | LastQuoteArrayResult |

## 6. Client-behavior notes from the vendor package (high)

- The vendor Python client is sync-over-async with a 60 s connect timeout
  and effectively unlimited frame size (`max_size=2**50`) — expect very
  large single frames on chain/snapshot calls.
- No heartbeat message appears in the vendor lib — keepalive is left to
  the WebSocket layer; server-side idle policy **UNVERIFIED**.
- The REST transport mirrors every non-Subscribe function above as
  `GET /{Function}/?accessKey=…` — REST-specific response differences
  (ALL-CAPS fields, millisecond epochs) are documented in
  [04-historical-data.md](./04-historical-data.md) §REST.

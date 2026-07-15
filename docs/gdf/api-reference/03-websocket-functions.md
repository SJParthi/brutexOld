# 03 — WebSockets API: function vocabulary, streams, quotes, wire shapes

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor section covered: **WebSockets API
documentation** (per-function pages). Primary evidence: **GFDLWS 0.0.4**
(vendor-authored PyPI sdist, uploaded 2024-09-16; predecessor `ws-gfdl`
1.1.0, 2022-08-12, near-identical README) — pypi-fetch = high confidence.

## 1. The function vocabulary

Vendor README "List of Functions" (verbatim order, high):

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

## 2. Streaming subscriptions — verbatim wire JSON (vendor source, high)

### SubscribeRealtime — 1-second push per instrument (runs until market close or unsubscribe)

Request (GFDLWS `realtime.py`):
```json
{"MessageType":"SubscribeRealtime","Exchange":"NFO","Unsubscribe":"false","InstrumentIdentifier":"NIFTY-I"}
```
Unsubscribe = the same message with `"Unsubscribe":"true"`.

Response stream — `MessageType: "RealtimeResult"` (README sample, verbatim):
```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1669262572,"ServerTime":1669262572,"AverageTradedPrice":18309.69,"BuyPrice":18323.9,"BuyQty":50,"Close":18286.75,"High":18329.35,"Low":18297.15,"LastTradePrice":18325.0,"LastTradeQty":750,"Open":18310.0,"OpenInterest":5657350,"QuotationLot":50.0,"SellPrice":18325.1,"SellQty":100,"TotalQtyTraded":681250,"Value":12473476312.5,"PreOpen":false,"PriceChange":38.25,"PriceChangePercentage":0.21,"OpenInterestChange":-98200,"MessageType":"RealtimeResult"}
```

Notes (high unless stated): timestamps are Unix epoch **seconds**;
top-of-book only (single Buy/Sell price+qty) — no depth levels in this
message. L2/market-depth stream: **UNVERIFIED** (no retrieved doc shows
one; the `Depth` parameter of SubscribeOptionChain is a chain-width
selector, not book depth). Update cadence: 1-second snapshots — GDF
"tick" data is second-stamped, not raw exchange tick-by-tick (inference
from vendor materials, medium — flagged).

### SubscribeSnapshot — streamed bar snapshots per Periodicity/Period (e.g. 1-min bars as they complete)

Request (`realsnapshot.py`):
```json
{"MessageType":"SubscribeSnapshot","Exchange":"NFO","Periodicity":"MINUTE","Period":1,"InstrumentIdentifier":"NIFTY-I"}
```
Response stream — `MessageType: "RealtimeSnapshotResult"` (README verbatim):
```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","Periodicity":"MINUTE","Period":1,"LastTradeTime":1669265340,"TradedQty":11300,"OpenInterest":5608700,"Open":18351.0,"High":18353.95,"Low":18350.25,"Close":18353.95,"MessageType":"RealtimeSnapshotResult"}
```

## 3. On-demand quote calls (single round-trip over the same WS)

### GetLastQuote
Params: Exchange, InstrumentIdentifier, optional isShortIdentifier
[true/false, default false]. Response `LastQuoteResult` (README verbatim):
```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"ServerTime":1658138400,"AverageTradedPrice":16234.07,"BuyPrice":16310.0,"BuyQty":4900,"Close":16068.3,"High":16520.0,"Low":16170.45,"LastTradePrice":16312.95,"LastTradeQty":800,"Open":16199.0,"OpenInterest":11179850,"QuotationLot":50.0,"SellPrice":16312.95,"SellQty":300,"TotalQtyTraded":8102000,"Value":131528435140.0,"PreOpen":false,"PriceChange":244.65,"PriceChangePercentage":1.52,"OpenInterestChange":-767300,"MessageType":"LastQuoteResult"}
```

### GetLastQuoteShort → `LastQuoteShortResult` (README verbatim)
```json
{"Exchange":"NFO","InstrumentIdentifier":"NIFTY-I","LastTradeTime":1658138400,"BuyPrice":16310.0,"LastTradePrice":16312.95,"SellPrice":16312.95,"MessageType":"LastQuoteShortResult"}
```

### GetLastQuoteShortWithClose → `LastQuoteShortWithCloseResult`
Same as Short + previous-day `Close`. (high)

### Array variants — GetLastQuoteArray / …Short / …ShortWithClose
- Max **25 symbols per call**; identifiers passed as a JSON array of
  `{"Value": …}` objects, e.g. `'[{"Value":"NIFTY-I"}, {"Value":"BANKNIFTY-I"}]'`.
- Envelopes: `LastQuoteArrayResult` / `LastQuoteArrayShortResult` /
  `LastQuoteArrayShortWithCloseResult` wrapping per-symbol records. (high)

### GetSnapshot — latest bar per Periodicity/Period, up to 25 symbols
Response `SnapshotResult`. NOTE: the response echoes FULL identifiers
(`FUTIDX_NIFTY_28JUL2022_XX_0`) even when the request used continuous
aliases (`NIFTY-I`) — README verbatim sample:
```json
{"InstrumentIdentifier":"FUTIDX_BANKNIFTY_28JUL2022_XX_0","Exchange":"NFO","LastTradeTime":1658204580,"TradedQty":31200,"OpenInterest":2306425,"Open":35436.5,"High":35443.0,"Low":35420.15,"Close":35434.0,"TokenNumber":null}
```

## 4. Common parameter table (vendor README verbatim, high)

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

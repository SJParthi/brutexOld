# GDFL WebSocket API — own-words summary

> **Decision:** DEC-GDFL-VENDOR-DOCS-001 (MIGRATION.md #1780).
> **Source of every fact on this page:** the vendor's own WebSocket API reference,
> captured 2026-07-13 from PyPI as the `ws-gfdl@1.1.0` long-description (author
> `Global Datafeeds <developer@globaldatafeeds.in>`; the same document family ships
> as the `wsgfdl-py` README), cross-checked against the shipped `wsgfdl-py@1.3.5`
> source code (the newest release in the family, 2025-11-28) — **summarized in our
> own words** per the SUMMARIZE-ONLY ruling (`../README.md`). MessageType/parameter/
> field NAMES are vendor identifiers kept verbatim; no vendor prose is reproduced.
> Re-fetch the verbatim originals via the pinned digests in `../sdk/PACKAGES.md`.

## Connect + Authenticate flow
*(source: vendor WS reference captured from PyPI ws-gfdl@1.1.0 / wsgfdl-py@1.3.5, summarized; code-verified in `gfdlws/ws.py`)*

- Endpoint is a **customer-issued** `ws://endpoint:port` — GDFL issues host + port
  per subscriber; no hostname is baked into any SDK.
- **TLS flag:** every example in the reference and the SDK uses plaintext
  `ws://`; whether the server accepts `wss://` is UNVERIFIED (probe gate 4 in
  `../VERDICT.md`).
- The FIRST frame the client sends is
  `{"MessageType": "Authenticate", "Password": "<API key>"}`; the server answers a
  frame with `MessageType: AuthenticateResult` carrying `Complete` (bool) and
  `Message`. The SDK re-sends once on `Complete: false`.
- All subsequent requests are JSON text frames keyed by `MessageType`; responses
  are JSON frames keyed by their own result `MessageType`.
- **Session policy:** 1 WS session per key (PRIOR-VERIFIED-NOTE, 2026-07-12 —
  not restated in the captured SDKs).
- **No heartbeat, ping, keep-alive, or reconnect logic exists anywhere in any
  captured SDK** (grep/AST-verified) — a production consumer must supply its own
  liveness + reconnect discipline. The SDK connects with a 60 s timeout and an
  effectively unbounded frame-size limit (history frames can be huge).

## Live streaming
*(source: vendor WS reference captured from PyPI ws-gfdl@1.1.0 / wsgfdl-py@1.3.5, summarized)*

- **`SubscribeRealtime`** → a stream of `RealtimeResult` frames at roughly one per
  second per subscribed instrument (the vendor reference states a 1-second
  frequency), running while the market is open. Works per-contract, including
  individual option contracts. Frame fields: `Exchange, InstrumentIdentifier,
  LastTradeTime, ServerTime, AverageTradedPrice, BuyPrice, BuyQty, Close, High,
  Low, LastTradePrice, LastTradeQty, Open, OpenInterest, QuotationLot, SellPrice,
  SellQty, TotalQtyTraded, Value, PreOpen, PriceChange, PriceChangePercentage,
  OpenInterestChange` — i.e. L1 bid/ask + LTP/LTQ + OI, the live twin of the
  1-second L1 snapshot data described in `../VERDICT.md`. Unsubscribe = the same
  message with `"Unsubscribe": "true"`.
- **`SubscribeSnapshot`** → `RealtimeSnapshotResult` per COMPLETED bucket at the
  requested `Periodicity`/`Period` (e.g. one frame per finished minute):
  `Exchange, InstrumentIdentifier, Periodicity, Period, LastTradeTime, TradedQty,
  OpenInterest, Open, High, Low, Close`.
- Additional subscription streams: `SubscribeRealtimeGreeks` (by numeric `Token`),
  `SubscribeOptionChain` / `SubscribeOptionChainGreeks` (by `Product`/`Expiry`/
  `StrikePrice`/`Depth`), `SubscribeSnapshotGreeks`, `SubscribeTopGainersLosers`.

## History over WS
*(source: vendor WS reference captured from PyPI ws-gfdl@1.1.0 / wsgfdl-py@1.3.5, summarized)*

- `GetHistory` request fields: `Exchange`, `InstrumentIdentifier`, `Periodicity`
  (`TICK`/`MINUTE`/`HOUR`/`DAY`/`WEEK`/`MONTH`, default `TICK` — the SAME enum as
  REST; **no 1-second periodicity**), `Period` (integer multiplier for
  MINUTE/HOUR/DAY), then `From`+`To` (Unix epoch SECONDS) or `Max` (latest-N);
  optional `isShortIdentifier`, `UserTag`.
- Responses arrive as `HistoryOHLCResult` (OHLC record fields: `LastTradeTime,
  QuotationLot, TradedQty, OpenInterest, Open, High, Low, Close`) or
  `HistoryTickResult` (1-second L1 snapshot record fields: `LastTradeTime,
  LastTradePrice, QuotationLot, TradedQty, OpenInterest, BuyPrice, BuyQty,
  SellPrice, SellQty`). WS samples carry `LastTradeTime` in epoch SECONDS (the
  REST samples show milliseconds — probe before relying on either).
- `GetHistoryAfterMarket`: same request shape; serves history up to the previous
  working day (current day only after close).
- `GetHistoryGreeks` → `HistoryGreeksResult` (same Greek field names as the REST
  page).
- `wsgfdl-py` ≥1.3.4 adds a split-adjusted history call carrying an
  `AdjustSplits` flag.

## Full MessageType inventory
*(source: wsgfdl-py@1.3.5 shipped code, AST-verified — 38 request MessageTypes / 36 result MessageTypes; the vendor's ws-gfdl@1.1.0 reference documents 30 numbered items — 29 functions + a Parameters section — the newer package added the rest)*

### Request MessageTypes (38) — one-line purpose each

| MessageType | Purpose |
|---|---|
| `Authenticate` | first-frame key auth |
| `SubscribeRealtime` | ~1/s L1 stream per instrument |
| `SubscribeSnapshot` | completed-bucket bar stream |
| `SubscribeRealtimeGreeks` | live Greeks stream by token |
| `SubscribeOptionChain` | live option-chain stream |
| `SubscribeOptionChainGreeks` | live chain-with-Greeks stream |
| `SubscribeSnapshotGreeks` | completed-bucket Greeks stream |
| `SubscribeTopGainersLosers` | live movers stream |
| `GetHistory` | history (the periodicity enum above) |
| `GetHistoryAfterMarket` | history up to previous working day |
| `GetHistoryGreeks` | historical Greeks series |
| `GetLastQuote` / `GetLastQuoteShort` / `GetLastQuoteShortWithClose` | single-symbol latest quote (full / short / short+close) |
| `GetLastQuoteArray` / `GetLastQuoteArrayShort` / `GetLastQuoteArrayShortWithClose` | multi-symbol latest quotes (max 25) |
| `GetLastQuoteOptionChain` | last-quote whole chain |
| `GetLastQuoteOptionGreeks` / `GetLastQuoteArrayOptionGreeks` | last Greeks by token (single / array) |
| `GetLastQuoteOptionGreeksChain` | last Greeks whole chain |
| `GetSnapshot` / `GetSnapshotGreeks` | latest completed bar(s) / Greeks snapshot on demand |
| `GetExchangeSnapshot` | whole-exchange snapshot |
| `GetExchanges` / `GetExchangeMessages` / `GetMarketMessages` | exchange list / exchange notices / market-status notices |
| `GetInstruments` / `GetInstrumentsOnSearch` | instrument master (list / search) |
| `GetInstrumentTypes` / `GetProducts` / `GetExpiryDates` / `GetOptionTypes` / `GetStrikePrices` | reference-token lists |
| `GetTopGainersLosers` | movers on demand |
| `GetVolumeShockers` | unusual-volume list (new in wsgfdl-py 1.3.5) |
| `GetLimitation` | the key's entitlement record (same fields as the REST page) |
| `GetServerInfo` | serving-backend ID |

### Result MessageTypes (36)

`AuthenticateResult` · `RealtimeResult` · `RealtimeSnapshotResult` ·
`RealtimeGreeksResult` · `RealtimeOptionChainResult` ·
`RealtimeOptionChainGreeksResult` · `RealtimeSnapshotGreeksResult` ·
`RealtimeGainersLosersResult` · `HistoryOHLCResult` · `HistoryTickResult` ·
`HistoryGreeksResult` · `LastQuoteResult` · `LastQuoteShortResult` ·
`LastQuoteShortWithCloseResult` · `LastQuoteArrayResult` ·
`LastQuoteArrayShortResult` · `LastQuoteArrayShortWithCloseResult` ·
`LastQuoteOptionGreeksResult` · `LastQuoteArrayOptionGreeksResult` ·
`OptionGreeksChainWithQuoteResult` · `SnapshotResult` · `SnapshotGreeksResult` ·
`ExchangeSnapshotResult` · `ExchangesResult` · `ExchangeMessagesResult` ·
`MarketMessagesResult` · `InstrumentsResult` · `InstrumentsOnSearchResult` ·
`InstrumentTypesResult` · `ProductsResult` · `ExpiryDatesResult` ·
`OptionTypesResult` · `StrikePricesResult` · `LastRealtimeVolumeShockersResult` ·
`LimitationResult` · `ServerInfoResult`

## Honesty notes for any future consumer

- **No heartbeat / no reconnect** in any SDK — disconnect behavior, gap profile
  and sustained-uptime characteristics are probe gates 5–6 in `../VERDICT.md`.
- **Plaintext `ws://` examples only** — TLS support UNVERIFIED (gate 4).
- The current package's own span-form history wrapper is broken as shipped
  (`../sdk/PACKAGES.md`) — a reminder that these SDKs are best consumed as
  protocol documentation, speaking the wire protocol directly.

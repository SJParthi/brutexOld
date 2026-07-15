# 06 — Greeks API + Advanced Streaming API (option chains & greeks)

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md). Vendor sections covered: **Greeks API**
(`…/greeks-api/{subscriberealtimegreeks, getlastquoteoptiongreekschain,
gethistorygreeks-2}/`) and **Advanced Streaming API**
(`…/advanced_streaming_api/subscribeoptionchain/`). Evidence: GFDLWS
0.0.4 modules + README (pypi-fetch, high) unless noted.

## 1. The Greeks vocabulary

GDF serves option Greeks keyed by the numeric instrument **TokenNumber**
(from GetInstruments / GetInstrumentsOnSearch — see
[05](./05-instruments-reference.md)), as:

| Function | Kind | Response |
|---|---|---|
| SubscribeRealtimeGreeks | 1-second stream per option token | `SubscribeRealtimeGreeks` (echoed name) |
| GetLastQuoteOptionGreeks | one token snapshot | `LastQuoteOptionGreeksResult` |
| GetLastQuoteArrayOptionGreeks | up to **25** tokens | `LastQuoteArrayOptionGreeksResult` |
| GetLastQuoteOptionGreeksChain | ENTIRE chain of an underlying (params Exchange, Product) | chain envelope — vendor sample only as ZIP `OptionGreeksChainWithQuoteResponse_JSON.zip` (not retrievable) |
| GetHistoryGreeks | historical Greeks rows (params Exchange, InstrumentIdentifier, isShortIdentifier, Max, From, To) | `HistoryGreeksResult` — see [04-historical-data.md](./04-historical-data.md) |

### SubscribeRealtimeGreeks — wire shape (README verbatim, high)

Request (built by `realtimegreeks.py`):
```json
{"MessageType":"SubscribeRealtimeGreeks","Exchange":"NFO","Token":"70222"}
```
Response stream (note MessageType echoes the REQUEST name):
```json
{"Exchange":"NFO","Token":"70222","Timestamp":1660292501,"IV":0.11267127841711044,"Delta":0.2549774944782257,"Theta":-6.706413269042969,"Vega":7.45473575592041,"Gamma":0.0012275002663955092,"IVVwap":0.1129087582230568,"Vanna":2.9635884761810303,"Charm":-0.026660971343517303,"Speed":3.0227099614421604E-06,"Zomma":-0.006058624479919672,"Color":-5.450446406030096E-05,"Volga":4533.580078125,"Veta":132.6668243408203,"ThetaGammaRatio":-5463.4716796875,"ThetaVegaRatio":-0.8996178507804871,"DTR":-0.038019951432943344,"MessageType":"SubscribeRealtimeGreeks"}
```

**Greek set (16 fields):** IV, Delta, Theta, Vega, Gamma, IVVwap, Vanna,
Charm, Speed, Zomma, Color, Volga, Veta, ThetaGammaRatio, ThetaVegaRatio,
DTR — notably richer than first-order-only vendors (TrueData's stream
carries IV/Delta/Theta/Gamma/Vega/Rho). Formula/inputs (rate source,
spot source, IV solver) — **UNVERIFIED** (no methodology doc retrieved).

## 2. Option chains

### GetLastQuoteOptionChain (snapshot)
LTP of the ENTIRE option chain of an underlying. Params: Exchange,
Product, [Expiry, OptionType, StrikePrice]. The vendor lib filters for
`LastQuoteArrayResult`; the vendor's full response sample exists only as
the ZIP `GetLastQuoteOptionChainResponse_JSON.zip` (not retrievable).

### SubscribeOptionChain (stream, "Advanced Streaming API")
Request (`subscribeoptionchain.py`, fields appended when non-None):
```json
{"MessageType":"SubscribeOptionChain","Exchange":"NFO","Product":"NIFTY","Expiry":"<expiry>","OptionType":"<CE|PE>","StrikePrice":"<strike>","Depth":"<n>","Unsubscribe":"false"}
```
Response stream: `RealtimeOptionChainResult`.

### SubscribeOptionChainGreeks (stream)
```json
{"MessageType":"SubscribeOptionChainGreeks","Exchange":"NFO","Unsubscribe":"false","Product":"NIFTY","Expiry":"<expiry>","StrikePrice":"<strike>","OptionType":"<CE|PE>","Depth":"<n>"}
```
Response stream: `RealtimeOptionChainGreeksResult`.

**`Depth` parameter caveat:** it selects chain WIDTH (strike count around
ATM), not order-book depth — exact semantics UNVERIFIED (the vendor page
was not fetchable).

## 3. Notes for BruteX / TickVault

- GDF's Greeks are VENDOR-COMPUTED server-side streams/snapshots. BruteX
  computes its own Greeks at enrich time (`brutex/data/greeks_enrich.py`,
  stamped + provenance-tracked); if GDF data were ever adopted, vendor
  Greeks would be REFERENCE_ONLY at best and never a substitute for the
  stamped enrichment chain (Golden Rule #1 — vendor formula UNVERIFIED).
- Historical Greeks (`GetHistoryGreeks`) depth/coverage: UNVERIFIED —
  probe alongside the history-depth questions in
  [04](./04-historical-data.md).
- Chain streams + the 25-token array cap shape any chain-capture design:
  whole-chain snapshots come from `GetLastQuoteOptionGreeksChain` /
  `GetLastQuoteOptionChain` (single large frames — the vendor client's
  `max_size=2**50` exists for a reason), while per-token streams scale by
  subscription count.

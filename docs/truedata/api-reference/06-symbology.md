# 06 — Symbol master & symbology

Researched 2026-07-15. Retrieval-honesty envelope + confidence legend:
[README.md](./README.md).

## 1. Symbol-master endpoints

| Claim | Value | Source (2026-07-15) | Confidence |
|---|---|---|---|
| Master download (API) | `GET https://api.truedata.in/getAllSymbols?segment=all&user=<u>&password=<p>&allexpiry=false` → `{Records: [[id, symbol, …], …]}`; also `&search=<symbol>` for lookup | truedata-nodejs `feed.js` + `historical.js` (pkg-source) | high |
| Per-segment prev-close/OI + id master | `GET https://api.truedata.in/getprevcloseandoi?segment=<seg>&user=&password=` for `EQ, FO, MCX, CDS, IN, BSEEQ, BSEFO` — the Python full-feed builds its symbol-id cache from this | truedata 7.0.1 `websocket/utils.py` (pkg-source) | high |
| Static symbol-list files | `https://www.truedata.in/downloads/symbol_lists/1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt` (numbered per-segment .txt files) | KB "Symbol Lists & Symbol Formats for TrueData Velocity 2.0" via search-relay (host blocked — file unretrieved) | medium |
| Master refresh cadence (client-side) | the Node full-feed SDK re-downloads symbol names daily around 02:40 UTC (~08:10 IST) before market open | pkg-source | high (client behavior) |

## 2. Naming conventions

| Instrument | Format | Example | Source | Confidence |
|---|---|---|---|---|
| NSE equity spot | plain trading symbol | `RELIANCE`, `SBIN`, `L&TFH` (raw `&`; the SDK URL-encodes `&`→`%26` via `specialSymbol()`) | npm README + historical.js (pkg-source) | high |
| Index spot | index NAME **with spaces** | `NIFTY 50`, `NIFTY BANK` | vendor blog via search-relay ("For Nifty 50, traders need to use the symbol 'NIFTY 50'"); vendor Python sample `get_historic_data("NIFTY BANK", …)` (github-fetch) | high |
| Continuous futures | `-I` / `-II` / `-III` suffix (current/near/far month); `_I` variants for platforms that reject `-` | `NIFTY-I`, `BANKNIFTY-I`, `CRUDEOIL-I`, `NIFTY-II` | npm/PyPI READMEs (direct-fetch) + blog via search-relay ("I, II, III stands for the months – Current, Near, Far") | high |
| Contract futures | `<SYMBOL><YY><MMM>FUT` | `NIFTY23OCTFUT` | blog example via search-relay | medium |
| Options (API/WS chain symbols) | `<SYMBOL><YYMMDD><STRIKE><CE|PE>` | expiry 2026-05-12 NIFTY 25000 call → `NIFTY26051225000CE` (generated `expiry.strftime('%y%m%d')` + strike + CE/PE) | truedata 7.0.1 `TD_chain.get_option_symbols` + truedata-nodejs `optionChain.js _generateSymbols` (pkg-source) | high |
| Options (platform/blog variant) | `<SYMBOL><STRIKE><C|P><digit>` | "Nifty Options with a strike price of 20000 … call option … `NIFTY20000C1`" | vendor blog via search-relay | **RECORDED CONFLICT** — likely the Velocity/desktop-platform shortcode (weekly-expiry serial digit) vs the API's YYMMDD format; both retained; platform-format semantics UNVERIFIED |
| MCX | same continuous-futures convention | `CRUDEOIL-I` | npm README (direct-fetch) | high |
| Platform restrictions | NinjaTrader: `-`/`&` replaced by `_`; MetaStock: 14-char symbol length limit | KB via search-relay | medium |

## 3. Segment vocabulary (across endpoints; pkg-source unless noted)

| Context | Values |
|---|---|
| Bhavcopy `segment` | `EQ, FO, MCX, BSEEQ, BSEFO` |
| Gainers/losers `topsegment` | `NSEEQ, NSEFUT, NSEOPT, CDS, MCX` |
| Full-feed master segments | `EQ, FO, MCX, CDS, IN, BSEEQ, BSEFO` (`IN` = indices — inference, low) |
| Exchange coverage (authorized) | NSE EQ / Indices / F&O / CDS (currency), BSE EQ / F&O / Indices, MCX ("Real-Time Data and Historical Data via their Market Data API for NSE Equity, NSE Indices, NSE Futures & Options, NSE Currency Derivatives and MCX" — truedata.in + KB via search-relay, medium) |

## 4. Symbol-ids

- A numeric `symbol_id` accompanies every tick/bar (the wire arrays key
  on it); the map comes from getAllSymbols / getprevcloseandoi. The Node
  SDK caches it to `symbolName.json` daily; Python to a pickle
  (`sym_cache_<ddmmyy>.pkl`). (pkg-source, high.)
- History REST can echo `symbolid` per row (`getSymbolId=1` in the Node
  SDK resolves it via a getAllSymbols search). (pkg-source, high.)

## 5. Mixed-symbol subscription evidence (vendor sample, github-fetch, high)

`option_chain_sample_code.py` subscribes
`["BANKNIFTY-I", "SBIN-I", "SBIN", "NIFTY 50"]` in ONE list (continuous
index future + stock future + cash stock + spot index) and starts option
chains for SBIN (stock), NIFTY/BANKNIFTY (index) and CRUDEOIL (MCX) —
chains work across NSE F&O and MCX underlyings.

## 6. INDIA VIX

Not directly observed in any retrieved list/sample. NSE indices are a
covered segment and a spot-index list file exists
(`1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt`), so India VIX availability is
PLAUSIBLE but **UNVERIFIED** — the exact TrueData symbol (e.g.
`INDIA VIX`) was unretrievable (the .txt was 403-blocked). Do NOT
hardcode; probe `getAllSymbols` live. (BruteX stamps `vix_at_entry/exit`
on every observable trade — CLAUDE.md §2 Rule 3 — so this is a required
probe item for any adapter.)

## 7. Mapping notes for BruteX

- TrueData spot-index names use SPACES (`NIFTY 50`, `NIFTY BANK`) —
  different from Groww's `NSE-NIFTY` / index trading-symbol style; the
  BruteX lake keys (`bars/<exch>/<segment>/<symbol>/…`) would need an
  explicit mapping layer, and the index-vs-derivatives name split
  (`NIFTY 50` spot vs `NIFTY` product vs `NIFTY-I` future) parallels the
  Groww-side alias handling in `brutex/universe.py`.
- The `&`-name behavior is EVIDENCED here (SDK URL-encodes `L&TFH` →
  `%26`) — unlike the Groww `%26` hypothesis history (CLAUDE.md §6),
  TrueData's own SDK performs the encoding, so raw-`&` symbols are the
  canonical spelling with transport-level encoding.

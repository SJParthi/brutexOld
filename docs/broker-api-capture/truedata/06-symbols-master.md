# TrueData — Symbol Formats & Symbol Master Lists

> Sources:
> - https://feedback.truedata.in/knowledge-base/article/symbols-format-for-websocket-api-velocity-3-0 (WS API formats)
> - https://feedback.truedata.in/knowledge-base/article/market-data-api-ws_api-symbol-lists (WS API symbol master downloads)
> - https://feedback.truedata.in/knowledge-base/article/symbol_lists (Velocity 2.0 symbol lists + sample formats)
> - https://www.nuget.org/packages/TrueData-DotNet/ (TDMaster class; `RELIANCE_BSE`-style symbols)
> - https://www.truedata.in/products/marketdataapi (long/short/continuous format examples)

## 1. Symbol formats for the Market Data (WebSocket) API

Official article (key points verbatim):

- Formats are unified between the WebSocket API and Velocity 3.0.
- **Weekly & Monthly options use the SAME format** (no more weekly-vs-monthly difference, no Oct–Dec alphabet codes).

### Options (weekly & monthly)

```
<ticker><expiry:YYMMDD><strike><option type CE|PE>
```

Examples (verbatim):

```
BANKNIFTY21031834500PE / BANKNIFTY21031834500CE  >> 18-Mar-2021 Weekly Expiry
BANKNIFTY21032534500PE / BANKNIFTY21032534500CE  >> 25-Mar-2021 Monthly Expiry
BANKNIFTY21040134500PE / BANKNIFTY21040134500CE  >> 01-APR-2021 Weekly Expiry
BANKNIFTY21042934500PE / BANKNIFTY21042934500CE  >> 29-APR-2021 Monthly Expiry
```

### Indices

Use exchange spot-index names with spaces (verbatim): `NIFTY 50, NIFTY BANK, INDIA VIX, NIFTY IT` (and `SENSEX` for BSE — seen in official .NET sample).

### Futures

- Continuous futures: `SYMBOL-I` / `SYMBOL-II` / `SYMBOL-III` (hyphen, underscore, and Roman numerals supported: `NIFTY-I`, `NIFTY_I`, `BANKNIFTY-II`, `CRUDEOIL-I`, `USDINR-I`).
- Contract futures: `<TICKER><YY><MMM>FUT` e.g. `NIFTY19OCTFUT`, `BANKNIFTY19OCTFUT`, `SBIN19OCTFUT` (Velocity format; product page also lists short `NIFTY25AUG22FUT` and long `FUTIDX_NIFTY_25AUG2022_XX_0` forms).

### Equities

Plain ticker for NSE (`SBIN`, `ACC`, `RELIANCE`, `L&TFH`); **BSE equities carry the `_BSE` suffix** in the WS API (official .NET sample: `RELIANCE_BSE`).

## 2. Symbol master lists — Market Data (WebSocket) API

Downloadable plain-text masters (official; www.truedata.in/downloads/symbol_lists/):

| # | List | URL |
|---|---|---|
| 1 | WEB_SOCKET_API_NSE_SPOT_INDEX | https://www.truedata.in/downloads/symbol_lists/1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt |
| 2 | WEB_SOCKET_API_ALL_NSE_EQ | https://www.truedata.in/downloads/symbol_lists/2.WEB_SOCKET_API_ALL_NSE_EQ.txt |
| 3 | WEB_SOCKET_API_ALL_NSE_CONTINUOUS_FUTURES | https://www.truedata.in/downloads/symbol_lists/3.WEB_SOCKET_API_ALL_NSE_CONTINUOUS_FUTURES.txt |
| 4 | WEB_SOCKET_API_NSE_CONTRACT_FUTURES | https://www.truedata.in/downloads/symbol_lists/4.WEB_SOCKET_API_NSE_CONTRACT_FUTURES.txt |
| 5 | WEB_SOCKET_API_NSE_INDICES_OPTIONS | https://www.truedata.in/downloads/symbol_lists/5.WEB_SOCKET_API_NSE_INDICES_OPTIONS.txt |
| 6 | WEB_SOCKET_API_NSE_ALL_OPTIONS | https://www.truedata.in/downloads/symbol_lists/6.WEB_SOCKET_API_NSE_ALL_OPTIONS.txt |
| 7 | WEB_SOCKET_API_NSE_INTEREST_CONTINUOUS_FUTURES | https://www.truedata.in/downloads/symbol_lists/7.WEB_SOCKET_API_NSE_INTEREST_CONTINUOUS_FUTURES.txt |
| 8 | WEB_SOCKET_API_NSE_CURRENCY_CONTINUOUS_FUTURES | https://www.truedata.in/downloads/symbol_lists/8.WEB_SOCKET_API_NSE_CURRENCY_CONTINUOUS_FUTURES.txt |
| 9 | WEB_SOCKET_API_NSE_CURRENCY_CONTRACT_FUTURES | https://www.truedata.in/downloads/symbol_lists/9.WEB_SOCKET_API_NSE_CURRENCY_CONTRACT_FUTURES.txt |
| 10 | WEB_SOCKET_API_NSE_INTEREST_OPTIONS | https://www.truedata.in/downloads/symbol_lists/10.WEB_SOCKET_API_NSE_INTEREST_OPTIONS.txt |
| 11 | WEB_SOCKET_API_NSE_CURRENCY_OPTIONS | https://www.truedata.in/downloads/symbol_lists/11.WEB_SOCKET_API_NSE_CURRENCY_OPTIONS.txt |
| 12 | WEB_SOCKET_API_MCX_CONTRACT_FUTURES | https://www.truedata.in/downloads/symbol_lists/12.WEB_SOCKET_API_MCX_CONTRACT_FUTURES.txt |
| 13 | WEB_SOCKET_API_MCX_CONTINUOUS_FUTURES | https://www.truedata.in/downloads/symbol_lists/13.WEB_SOCKET_API_MCX_CONTINUOUS_FUTURES.txt |
| 14 | WEB_SOCKET_API_MCX_OPTIONS | https://www.truedata.in/downloads/symbol_lists/14.WEB_SOCKET_API_MCX_OPTIONS.txt |
| 15 | WEB_SOCKET_API_BSE_INDICES_OPTIONS | https://www.truedata.in/downloads/symbol_lists/15.WEB_SOCKET_API_BSE_INDICES_OPTIONS.txt |
| 16 | WEB_SOCKET_API_BSE_INDEX | https://www.truedata.in/downloads/symbol_lists/16.WEB_SOCKET_API_BSE_INDEX.txt |
| 17 | WEB_SOCKET_API_ALL_BSE_EQ | https://www.truedata.in/downloads/symbol_lists/17.WEB_SOCKET_API_ALL_BSE_EQ.txt |

## 3. Symbol master lists — Velocity 2.0 (TA applications)

Usage notes (verbatim): all lists work everywhere except NinjaTrader & Metastock; NinjaTrader does not accept `-` and `&` (replaced by `_`); Metastock max symbol length 14 chars; select-all with Alt+A, copy with Alt+C into the Velocity Add Symbol(s) box.

| Sr | List | URL |
|---|---|---|
| A | NSE INDICES | https://www.truedata.in/downloads/symbol_lists/A.NSE_INDICES.txt |
| B | MCX INDICES | https://www.truedata.in/downloads/symbol_lists/B.MCX_INDICES.txt |
| C | BSE INDICES | https://www.truedata.in/downloads/symbol_lists/C.BSE_INDICES.txt |
| D | NSE CONTINUOUS OPTIONS | https://www.truedata.in/downloads/symbol_lists/D.NSE_CONTINUOUS_OPTIONS.txt |
| E | NSE CONTINUOUS OPTIONS METASTOCK | https://www.truedata.in/downloads/symbol_lists/E.NSE_CONTINUOUS_OPTIONS_METASTOCK.txt |
| F | BSE CONTINUOUS OPTIONS | https://www.truedata.in/downloads/symbol_lists/F.BSE_CONTINUOUS_OPTIONS.txt |
| 1 | NIFTY 50 | https://www.truedata.in/downloads/symbol_lists/1.NIFTY_50_EQ.txt |
| 2 | NIFTY NEXT 50 | https://www.truedata.in/downloads/symbol_lists/2.NIFTY_NEXT_50_EQ.txt |
| 3 | NIFTY 100 | https://www.truedata.in/downloads/symbol_lists/3.NIFTY_100_EQ.txt |
| 4 | NIFTY 200 | https://www.truedata.in/downloads/symbol_lists/4.NIFTY_200_EQ.txt |
| 5 | ALL NSE EQUITIES | https://www.truedata.in/downloads/symbol_lists/5.ALL_NSE_EQ.txt |
| 6 | NSE NIFTY 50 FUTURES CONTINUOUS CURRENT MONTH (-I) | https://www.truedata.in/downloads/symbol_lists/6.NSE_NIFTY_50_FUTURES_CONTINUOUS-I.txt |
| 7 | NSE NIFTY 50 FUTURES CONTINUOUS (_I, NinjaTrader) | https://www.truedata.in/downloads/symbol_lists/7.NSE_NIFTY_50_FUTURES_CONTINUOUS_NINJA_I.txt |
| 8 | NSE FUTURES CONTINUOUS CURRENT MONTH (-I) | https://www.truedata.in/downloads/symbol_lists/8.NSE_FUTURES_CONTINUOUS-I.txt |
| 9 | NSE FUTURES CONTINUOUS (_I, NinjaTrader) | https://www.truedata.in/downloads/symbol_lists/9.NSE_FUTURES_CONTINUOUS_NINJA_I.txt |
| 10 | NSE FUTURES CONTRACTS | https://www.truedata.in/downloads/symbol_lists/10.NSE_FUTURES_CONTRACTS.txt |
| 11 | NSE FUTURES CONTRACTS (Metastock) | https://www.truedata.in/downloads/symbol_lists/11.NSE_FUTURES_CONTRACTS_METASTOCK.txt |
| 12 | NSE INDICES OPTIONS | https://www.truedata.in/downloads/symbol_lists/12.NSE_INDICES_OPTIONS.txt |
| 13 | NSE ALL OPTIONS | https://www.truedata.in/downloads/symbol_lists/13.NSE_ALL_OPTIONS.txt |
| 14 | NSE ALL OPTIONS (Metastock) | https://www.truedata.in/downloads/symbol_lists/14.NSE_ALL_OPTIONS_METASTOCK.txt |
| 15 | MCX FUTURES CONTINUOUS CURRENT MONTH (-I) | https://www.truedata.in/downloads/symbol_lists/15.MCX_FUTURES_CONTINUOUS-I.txt |
| 16 | MCX FUTURES CONTINUOUS (_I, NinjaTrader) | https://www.truedata.in/downloads/symbol_lists/16.MCX_FUTURES_CONTINUOUS_NINJA_I.txt |
| 17 | MCX FUTURES CONTRACTS | https://www.truedata.in/downloads/symbol_lists/17.MCX_FUTURES_CONTRACTS.txt |
| 18 | MCX FUTURES CONTRACTS (Metastock) | https://www.truedata.in/downloads/symbol_lists/18.MCX_FUTURES_CONTRACTS_METASTOCK.txt |
| 19 | MCX OPTIONS | https://www.truedata.in/downloads/symbol_lists/19.MCX OPTIONS.txt |
| 20 | MCX OPTIONS (Metastock) | https://www.truedata.in/downloads/symbol_lists/20.MCX_OPTIONS_METASTOCK.txt |
| 21 | NSE CURRENCY CONTRACT FUTURES | https://www.truedata.in/downloads/symbol_lists/21.NSE_CURRENCY_CONTRACT_FUTURE.txt |
| 22 | NSE_CURRENCY_CONTINUOUS_FUTURES-I to -III | https://www.truedata.in/downloads/symbol_lists/22.NSE_CURRENCY_CONTINUOUS_FUTURES-Ito-III.txt |
| 23 | NSE_CURRENCY_CONNTINUOUS_FUTURE_ALL | https://www.truedata.in/downloads/symbol_lists/23.NSE_CURRENCY_CONNTINUOUS_FUTURE_ALL.txt |
| 24 | NSE CURRENCY INTEREST OPTIONS | https://www.truedata.in/downloads/symbol_lists/24.NSE_CURRENCY_INTEREST_OPTIONS.txt |
| 25 | NSE CURRENCY ALL OPTIONS | https://www.truedata.in/downloads/symbol_lists/25.NSE_CURRENCY_ALL_OPTIONS.txt |
| 26 | NSE_CURRENCY_INTEREST_OPTIONS_METASTOCK | https://www.truedata.in/downloads/symbol_lists/26.NSE_CURRENCY_INTEREST_OPTIONS_METASTOCK.txt |
| 27 | NSE_CURRENCY_ALL_OPTIONS_METASTOCK | https://www.truedata.in/downloads/symbol_lists/27.NSE_CURRENCY_ALL_OPTIONS_METASTOCK.txt |
| 28 | BSE_ALL_CONTRACT_FUTURES | https://www.truedata.in/downloads/symbol_lists/28.BSE_ALL_CONTRACT_FUTURES.txt |
| 29 | BSE_ALL_OPTIONS_METASTOCK | https://www.truedata.in/downloads/symbol_lists/29.BSE_ALL_OPTIONS_METASTOCK.txt |
| 30 | BSE_ALL_OPTIONS | https://www.truedata.in/downloads/symbol_lists/30.BSE_ALL_OPTIONS.txt |
| 31 | ALL BSE DEBT INSTRUMENTS | https://www.truedata.in/downloads/symbol_lists/31.ALL_BSE_DEBT_INSTRUMENTS.txt |
| 32 | ALL BSE EQ | https://www.truedata.in/downloads/symbol_lists/32.ALL_BSE_EQ.txt |
| 33 | ALL_BSE_GOVT_SECURITIES | https://www.truedata.in/downloads/symbol_lists/33.ALL_BSE_GOVT_SECURITIES.txt |
| 34 | ALL_BSE_MUTUAL_FUNDS | https://www.truedata.in/downloads/symbol_lists/34.ALL_BSE_MUTUAL_FUNDS.txt |
| 35 | BSE FUTURES CONTINUOUS-I | https://www.truedata.in/downloads/symbol_lists/35.BSE_FUTURES_CONTINUOUS-I.txt |
| 36 | BSE FUTURES CONTINUOUS NINJA_I | https://www.truedata.in/downloads/symbol_lists/36.BSE_FUTURES_CONTINUOUS_NINJA_I.txt |
| 37 | BSE WEEKLY NONCONTINUOUS CONTRACT FUTURES | https://www.truedata.in/downloads/symbol_lists/37.BSE_WEEKLY_NONCONTINUOUS_CONTRACT_FUTURES.txt |
| 38 | BSE MONTHLY CONTRACT FUTURES | https://www.truedata.in/downloads/symbol_lists/38.BSE_MONTHLY_CONTRACT_FUTURES.txt |
| 39 | BSE INDICES OPTIONS | https://www.truedata.in/downloads/symbol_lists/39.BSE_INDICES_OPTIONS.txt |
| 40 | BSE INDICES OPTIONS METASTOCK | https://www.truedata.in/downloads/symbol_lists/40.BSE_INDICES_OPTIONS_METASTOCK.txt |
| 41 | BSE_ALL_CONTRACT_FUTURES_METASTOCK | https://www.truedata.in/downloads/symbol_lists/41.BSE_ALL_CONTRACT_FUTURES_METASTOCK.txt |

## 4. Per-platform sample symbol formats (Velocity 2.0 article, verbatim)

### Amibroker / MotiveWave etc.

- Continuous futures: `NIFTY_I / NIFTY-I`, `BANKNIFTY_II / BANKNIFTY-II`, `L&TFH_I / L&TFH-I` (hyphen, underscore, Roman numerals I/II/III all supported)
- Contract futures: `NIFTY19OCTFUT`, `BANKNIFTY19OCTFUT`, `SBIN19OCTFUT`
- Options (weekly & monthly): `<ticker><YY><MM><DD><strike>CE|PE` — `BANKNIFTY221006237000CE` (06-Oct-2022), `BANKNIFTY22110337000PE` (03-Nov-2022), `BANKNIFTY221027237000CE` (27-Oct-2022), `BANKNIFTY22112437000PE` (24-Nov-2022)
- Equity: `L_TFH & L&TFH`, `SBIN`, `ACC`; Indices: `NIFTY`, `BANKNIFTY`, `CNX100`

### NinjaTrader 7 & 8

- No hyphen/space/ampersand: `NIFTY_I`, `BANKNIFTY_II`, `L_TFH_I`, `M_M_I`, `M_MFIN_I`
- Contract futures: `NIFTY20OCTFUT` etc.
- Options: months Jan–Dec as 1–12 without zero-padding: `BANKNIFTY2210637000CE` (06-Jan-2022), `BANKNIFTY2220337000CE` (03-Feb-2022), `BANKNIFTY22102737000CE` (27-Oct-2022), `BANKNIFTY22112437000CE` (24-Nov-2022)
- Indices need `^`: `^NIFTY`, `^BANKNIFTY`, `^CNX100`

### Metastock

- Underscore + Roman numerals: `NIFTY_I`, `BANKNIFTY_II`, `L&TFH_I`
- Contract futures: `NIFTY19OCTFUT` etc.
- Options monthly: `SBINJ9150`, `ACCJ9150`; weekly: `BANKNIFJ925200`, `NIFTYJ1712800` (14-char limit)
- Detailed format doc: https://www.truedata.in/feedback/knowledge-base/article/symbols-format-truedata-velocity

## 5. Programmatic symbol master (.NET)

```csharp
TDMaster tDMaster;                                  // Truedata Master
tDMaster = new TDMaster("your_id", "your_pwd");     // To download latest master
```

The master maps `symbol ↔ symbolid` (the .NET sample resolves incoming ticks via `scrips.Where(v => v.symbolid == t.SymbolId)`). Python `truedata` similarly maps feeds "with symbolid mapping" (truedata-ws 5.0.3 note). Segment master note: "Index segment masters IND updated to IN" (truedata-ws 4.3.5). Symbol renames: REST `getSymbolNameChange`.

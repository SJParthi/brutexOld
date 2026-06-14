<!--
  REPO PROVENANCE (DEC-MASTER-CONTEXT-PERSIST-001, persisted 2026-06-14):
  Operator-supplied external-sources REFERENCE — persisted verbatim from the
  master context the operator compiled + verified 2026-06-13. This is a
  REFERENCE, NOT a Track-1 spec: the authoritative specs remain the
  docs/TRACK1_*.md set (CLAUDE.md §2 r2). Every URL here is "verify on first
  run" (the doc's own Part 5 gotcha #3 + the verification-status table mark the
  unverified rows honestly — Golden Rule #1). Contains credential PLACEHOLDERS
  only (YOUR_API_KEY / YOUR_TOTP_SECRET / …), never real secrets. Where this
  doc and the repo's own verified facts (CLAUDE.md §6 / docs/groww/) disagree,
  the repo's file:line-cited facts win.
-->

# MASTER CONTEXT — Groww SDK + NSE/BSE Market Data

> **Single drop-in context file for a Claude Code session.** Combines: (1) the Groww Trading API Python SDK surface, and (2) NSE/BSE end-of-day market-data sources (spot + F&O + indices + SENSEX).
> **Compiled & verified:** 13 June 2026. Sources checked live this session.
> **Standing assumptions for the Code session:** prices in ₹; NSE blocks bare HTTP (cookie dance required); archive URLs rotate (treat every raw URL as "verify on first run"); EOD bhavcopy ≠ intraday (intraday comes from the Groww candles API).

---

# TABLE OF CONTENTS
- **PART 1 — Groww Python SDK** (auth, instruments, backtesting candles, enums, exceptions)
- **PART 2 — NSE market data** (spot CM, F&O, indices)
- **PART 3 — BSE market data** (spot equity, SENSEX index, F&O [unverified])
- **PART 4 — Unified Python fetcher** (one module, all segments)
- **PART 5 — Gotchas + verification status**

---
---

# PART 1 — GROWW PYTHON SDK

**Package:** `pip install growwapi` · **Python 3.9+** · Latest seen: `1.5.0` (3 Dec 2025).
Supports equity (CASH), derivatives (FNO), commodities (COMMODITY) across NSE/BSE/MCX.

## 1.1 Authentication (two flows)

**API Key + Secret** (requires daily approval):
```python
from growwapi import GrowwAPI
access_token = GrowwAPI.get_access_token(api_key="YOUR_API_KEY", secret="YOUR_API_SECRET")
groww = GrowwAPI(access_token)
```

**TOTP** (no expiry; needs `pip install pyotp`):
```python
from growwapi import GrowwAPI
import pyotp
totp = pyotp.TOTP("YOUR_TOTP_SECRET").now()
access_token = GrowwAPI.get_access_token(api_key="YOUR_TOTP_TOKEN", totp=totp)
groww = GrowwAPI(access_token)
```

## 1.2 Rate limits (per *type*, shared across all APIs in that type)
| Type | Per second | Per minute |
| --- | --- | --- |
| Orders (create/modify/cancel) | 10 | 250 |
| Live Data (quote/LTP/OHLC) | 10 | 300 |
| Non-Trading (status/positions/holdings/margin) | 20 | 500 |

Live feed: up to **1000 subscriptions** at a time.

## 1.3 Instruments
- Master CSV: `https://growwapi-assets.groww.in/instruments/instrument.csv`
- Lookups:
```python
groww.get_instrument_by_groww_symbol(groww_symbol="NSE-RELIANCE")
groww.get_instrument_by_exchange_and_trading_symbol(exchange=groww.EXCHANGE_NSE, trading_symbol="RELIANCE")
groww.get_instrument_by_exchange_token(exchange_token="2885")
groww.get_all_instruments()           # pandas DataFrame
groww._load_instruments()             # advanced
groww.INSTRUMENT_CSV_URL              # CSV url attribute
```
- CSV columns: `exchange, exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, underlying_exchange_token, lot_size, expiry_date, strike_price, tick_size, freeze_quantity, is_reserved, buy_allowed, sell_allowed` (DataFrame adds `internal_trading_symbol`).

## 1.4 Groww Symbol construction (for backtesting APIs)
Concatenate: Exchange − TradingSymbol [− ExpiryDate(DDMmmYY) − StrikePrice − OptionType(CE/PE/FUT)].
- Equity: `NSE-WIPRO` · Index: `NSE-NIFTY`
- Future: `NSE-NIFTY-30Sep25-FUT`
- Option: `NSE-NIFTY-30Sep25-24650-CE` / `...-PE`

## 1.5 Backtesting / historical APIs (intraday capable)
> Backtesting APIs support **CASH and FNO only**. Equities/Indices/FNO data from **2020**.

```python
# Expiries (FNO)
groww.get_expiries(exchange=groww.EXCHANGE_NSE, underlying_symbol="NIFTY", year=2024, month=1)
# -> {"expiries": ["2024-01-25", ...]}

# Contracts (FNO) for an expiry
groww.get_contracts(exchange=groww.EXCHANGE_NSE, underlying_symbol="NIFTY", expiry_date="2025-01-25")
# -> {"contracts": ["NSE-NIFTY-02Jan25-28500-PE", ...]}

# Historical candles (CASH or FNO)
groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,            # or SEGMENT_FNO
    groww_symbol="NSE-WIPRO",
    start_time="2025-09-24 10:56:00",      # 'yyyy-MM-dd HH:mm:ss' or epoch seconds
    end_time="2025-09-24 12:00:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_30,
)
```
**Candle tuple order:** `[timestamp, open, high, low, close, volume, open_interest]` (OI null for non-FNO).
Response also: `closing_price, start_time, end_time, interval_in_minutes`.

**Per-request duration caps:** 1–5 min → 30 days; 10/15/30 min → 90 days; 1h/4h/1d/1wk/1mo → 180 days. Chunk longer ranges.

## 1.6 Annexures (enums) — quick reference
- **Exchange:** `EXCHANGE_NSE`=NSE, `EXCHANGE_BSE`=BSE, `EXCHANGE_MCX`=MCX
- **Segment:** `SEGMENT_CASH`=CASH, `SEGMENT_FNO`=FNO, `SEGMENT_COMMODITY`=COMMODITY
- **Order type:** `ORDER_TYPE_LIMIT`=LIMIT, `ORDER_TYPE_MARKET`=MARKET, `ORDER_TYPE_STOP_LOSS`=SL, `ORDER_TYPE_STOP_LOSS_MARKET`=SL_M
- **Product:** `PRODUCT_CNC`=CNC, `PRODUCT_MIS`=MIS, `PRODUCT_NRML`=NRML
- **Transaction:** `TRANSACTION_TYPE_BUY`=BUY, `TRANSACTION_TYPE_SELL`=SELL
- **Validity:** `VALIDITY_DAY`=DAY
- **Candle interval:** `CANDLE_INTERVAL_MIN_1/2/3/5/10/15/30`=`Nminute`, `CANDLE_INTERVAL_HOUR_1`=1hour, `CANDLE_INTERVAL_HOUR_4`=4hour, `CANDLE_INTERVAL_DAY`=1day, `CANDLE_INTERVAL_WEEK`=1week, `CANDLE_INTERVAL_MONTH`=1month
- **Instrument type:** EQ, IDX, FUT, CE, PE
- **Order status values:** NEW, ACKED, TRIGGER_PENDING, APPROVED, REJECTED, FAILED, EXECUTED, DELIVERY_AWAITED, CANCELLED, CANCELLATION_REQUESTED, MODIFICATION_REQUESTED, COMPLETED

## 1.7 Exceptions (module `growwapi.groww.exceptions`)
`GrowwBaseException(msg)` → base. `GrowwAPIException(msg, code)` → client errors, with subclasses: `GrowwAPIAuthenticationException`, `GrowwAPIAuthorisationException`, `GrowwAPIBadRequestException`, `GrowwAPINotFoundException`, `GrowwAPIRateLimitException`, `GrowwAPITimeoutException`. Feed: `GrowwFeedException(msg)`, `GrowwFeedConnectionException(msg)`, `GrowwFeedNotSubscribedException(msg, topic)`.

## 1.8 Naming history (old code traps)
`GrowwClient` → **`GrowwAPI`**; `get_latest_price_data` → **`get_quote`** (both since 0.0.3). Commodity/MCX support added 1.5.0; `get_user_profile` 1.4.0; Option Chain 1.3.0; Smart Orders GTT/OCO 1.2.0; historical data APIs 1.0.0; TOTP 0.0.8.

> Full per-page Groww docs (instruments, backtesting, annexures, exceptions, changelog) exist as separate files `01_…`–`06_…` if you need the exhaustive per-page version.

---
---

# PART 2 — NSE MARKET DATA (EOD bhavcopy)

**The 8 July 2024 UDiFF switch governs everything:** dates **≥ 2024-07-08** use UDiFF URLs; **< 2024-07-08** use legacy URLs. `jugaad-data` auto-handles both.

## 2.1 NSE SPOT — Equity / Cash Market (CM) = "entire NSE equity bhavcopy"
**Current (UDiFF):**
```
https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
```
**Legacy (< 2024-07-08):**
```
https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip
```
- `<MON>`=UPPERCASE 3-letter month. `.zip` → one CSV inside. ✅ both verified.
- Friendly columns: `SYMBOL, SERIES(EQ/BE…), OPEN, HIGH, LOW, CLOSE, PREVCLOSE, TOTTRDQTY(vol), TOTTRDVAL(turnover), DELIV_PER(delivery %)`. (UDiFF raw uses long names `TckrSymb/ClsPric/…` — print `df.columns` once and map.)

## 2.2 NSE F&O — Futures + Options (one file = both, all strikes)
**Current (UDiFF):**
```
https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
```
**Legacy (< 2024-07-08):**
```
https://nsearchives.nseindia.com/content/historical/DERIVATIVES/<YYYY>/<MON>/fo<DD><MON><YYYY>bhav.csv.zip
```
- UDiFF FO URL ✅ verified (jugaad #79). Legacy FO ⚠️ pattern-confirmed via equity analogue — smoke-test.
- UDiFF FO key columns: `FinInstrmTp`(STF/IDF/STO/IDO), `OptnTp`(CE/PE), `TckrSymb`, `XpryDt`, `StrkPric`, `OpnPric/HghPric/LwPric/ClsPric`, `SttlmPric`, `OpnIntrst`, `ChngInOpnIntrst`, `TtlTradgVol`, `TtlTrfVal`.
- Legacy FO columns: `INSTRUMENT, SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP, OPEN, HIGH, LOW, CLOSE, SETTLE_PR, CONTRACTS, VAL_INLAKH, OPEN_INT, CHG_IN_OI, TIMESTAMP`.

## 2.3 NSE Indices (NIFTY 50, Bank Nifty, etc. — close values)
```
https://nsearchives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv
```
- Plain `.csv` (not zipped). Date token `DDMMYYYY` (note: different from UDiFF's `YYYYMMDD`).
- ⚠️ verify-first (NSE's long-stable indices path; smoke-test before production).

---
---

# PART 3 — BSE MARKET DATA

## 3.1 BSE SPOT — Equity / Cash (all BSE stocks incl. SENSEX constituents)
**Current:**
```
https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_<DDMMYY>_T0.CSV
```
**Legacy/zipped fallback:**
```
http://www.bseindia.com/download/BhavCopy/Equity/EQ<DDMMYY>_CSV.zip
```
- Plain `.CSV` (uppercase). Date token **`DDMMYY`** (2-digit year). ✅ live URL verified.
- Columns: `SYMBOL, NAME, SC_GROUP, SC_TYPE, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, NO_TRADES, VOLUME, NET_TURNOV, TDCLOINDI` (newer files add ISIN — inspect columns).

## 3.2 BSE SENSEX — the index OHLC (not constituents)
**RECOMMENDED — symbol-based, sturdy for backtest feed:**
```python
import yfinance as yf            # pip install yfinance
sensex = yf.download("^BSESN", start="2023-01-01", end="2026-06-13", interval="1d")
# Columns: Open, High, Low, Close, Adj Close, Volume ; index=Date
```
- SENSEX symbol **`^BSESN`** (Yahoo) / `BSESN.NS`. ✅ verified. History to 1980s–90s. Sidesteps BSE anti-bot.
- Index Volume is often 0/NaN — expected (it's an index).

**OFFICIAL (fragile) — BSE Index Archive:**
- Page (live): `https://www.bseindia.com/Indices/IndexArchiveData`
- Backed by a BSE API under `api.bseindia.com` (IndexArchiveData / ProduceCSVForDate family). Params/headers rotate; needs browser UA + `Referer: https://www.bseindia.com/`; actively anti-scraped. ⚠️ verify-first — use only if you need BSE's own numbers; otherwise prefer `^BSESN`.

## 3.3 BSE F&O — ⚠️ UNVERIFIED APPENDIX (you'd earlier scoped this out)
BSE derivatives **did** move to UDiFF, so the likely pattern (BY ANALOGY, NOT CONFIRMED this session):
```
https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_<YYYYMMDD>_F_0000.CSV
```
- 🚫 **Do not trust this URL blind** — I could not confirm the exact BSE FO download path live. If you actually trade BSE Sensex/Bankex options, tell me and I'll verify the precise endpoint + columns before you wire it in. Until then, treat BSE F&O as **not covered**.

---
---

# PART 4 — UNIFIED PYTHON FETCHER (all NSE/BSE EOD segments)

```python
import requests, io, zipfile, time
from datetime import date, timedelta
import pandas as pd

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}
SWITCH = date(2024, 7, 8)   # UDiFF cutover

def nse_session():
    s = requests.Session(); s.headers.update(NSE_HEADERS)
    s.get("https://www.nseindia.com", timeout=10)        # prime cookies (mandatory)
    return s

def _nse_url(seg: str, d: date) -> str:
    mon = d.strftime("%b").upper()
    if seg == "cm":
        return (f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
                if d >= SWITCH else
                f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip")
    if seg == "fo":
        return (f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
                if d >= SWITCH else
                f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/fo{d:%d}{mon}{d:%Y}bhav.csv.zip")
    if seg == "idx":
        return f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
    raise ValueError(seg)

def fetch_nse(seg: str, d: date, sess=None):
    sess = sess or nse_session()
    r = sess.get(_nse_url(seg, d), timeout=20)
    if r.status_code == 404:
        return None                                      # weekend/holiday
    r.raise_for_status()
    if r.content[:2] == b"PK":                           # zip
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open(z.namelist()[0]) as f:
                return pd.read_csv(f)
    return pd.read_csv(io.BytesIO(r.content))            # plain csv (indices)

def fetch_bse_equity(d: date):
    url = f"https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{d:%d%m%y}_T0.CSV"
    h = {**NSE_HEADERS, "Referer": "https://www.bseindia.com/"}
    r = requests.get(url, headers=h, timeout=20)
    return None if r.status_code == 404 else pd.read_csv(io.BytesIO(r.content))

def fetch_sensex(start: str, end: str):
    import yfinance as yf
    return yf.download("^BSESN", start=start, end=end, interval="1d")

def pull_range(seg: str, start: date, end: date) -> pd.DataFrame:
    """seg in {'cm','fo','idx'} (NSE) or 'bse_eq'. SENSEX index -> use fetch_sensex()."""
    sess = nse_session() if seg in {"cm", "fo", "idx"} else None
    frames, d = [], start
    while d <= end:
        if d.weekday() < 5:                              # skip Sat/Sun (holidays 404 -> handled)
            try:
                df = fetch_nse(seg, d, sess) if seg in {"cm","fo","idx"} else fetch_bse_equity(d)
                if df is not None:
                    df["TRADE_DATE"] = d.isoformat(); frames.append(df)
            except Exception as e:
                print(f"skip {seg} {d}: {e}")
            time.sleep(0.4)                              # be polite -> avoid bans
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

# Examples:
# nse_fno_3mo = pull_range("fo", date(2026,3,13), date(2026,6,13))
# nse_spot    = pull_range("cm", date(2026,3,13), date(2026,6,13))
# sensex      = fetch_sensex("2023-01-01", "2026-06-13")
```

### Sturdier alternative for backfills — `jugaad-data` (`pip install jugaad-data`)
```python
from datetime import date
from jugaad_data.nse import bhavcopy_save, bhavcopy_fo_save   # auto old/new era + cookies + cache
bhavcopy_save(date(2026,6,12), "./data/cm")
bhavcopy_fo_save(date(2026,6,12), "./data/fo")
```

---
---

# PART 5 — GOTCHAS + VERIFICATION STATUS

### Operating rules to bake in
1. **NSE blocks bare requests** → prime cookies via `nseindia.com`, then send browser headers.
2. **Weekends/holidays = HTTP 404**, not an error → skip. Keep an exchange holiday list to avoid false-error spam.
3. **8 Jul 2024 UDiFF switch** is the #1 backfill trap (NSE CM + FO) → branch on date or use `jugaad-data`.
4. **Date tokens differ:** NSE UDiFF `YYYYMMDD`; NSE legacy `DD<MON>YYYY`; NSE indices `DDMMYYYY`; BSE equity `DDMMYY`; BSE archive params `DD/MM/YYYY`.
5. **Publish timing:** today's bhavcopy appears only after market close (~evening IST).
6. **Be polite:** one session, `sleep(~0.4s)`, cache to disk — NSE throttles/bans hammering.
7. **EOD vs intraday:** Parts 2–3 are daily summaries (incl. OI for FO). Intraday candles come from the **Groww `get_historical_candles`** API (Part 1.5). Two different data layers — don't conflate.
8. **Cross-key symbols** between bhavcopy universe and Groww instrument `trading_symbol`/`groww_symbol` so your data and order paths line up.

### What was verified this session
| Item | Status |
| --- | --- |
| NSE CM UDiFF + legacy URLs | ✅ verified |
| NSE FO UDiFF URL | ✅ verified (jugaad #79) |
| NSE FO legacy URL | ⚠️ pattern-confirmed; smoke-test |
| NSE indices `ind_close_all` | ⚠️ verify-first |
| BSE equity `EQ_ISINCODE_…_T0.CSV` | ✅ live URL verified |
| BSE SENSEX `^BSESN` (yfinance) | ✅ verified |
| BSE Index Archive page | ✅ page live; underlying API params ⚠️ verify-first |
| **BSE F&O download URL** | 🚫 **UNVERIFIED** — do not trust blind; ask me to confirm |
| Groww SDK surface | ✅ from official docs (v1.5.0, Dec 2025) |

### Coverage summary
**Fully covered & verified:** NSE spot, NSE F&O, NSE indices, BSE spot, BSE SENSEX index, Groww SDK (auth/instruments/backtesting/enums/exceptions).
**Not trustworthy yet:** BSE F&O (appendix only).
**Staleness note:** Groww docs captured at v1.5.0 (Dec 2025); it's Jun 2026 — run `pip index versions growwapi` to confirm nothing newer shifted method signatures.

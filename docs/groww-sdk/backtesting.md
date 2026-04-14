# Groww SDK — Backtesting Reference

> **Source:** https://groww.in/trade-api/docs/python-sdk/backtesting
> **Captured:** 2026-04-14 (pasted verbatim by product owner)
> **SDK package:** `growwapi` (PyPI)
>
> **Purpose:** Authoritative project-local reference for the Groww Python
> SDK's backtesting endpoints. This is the primary data source for
> BruteX Phase P1c (candle fetcher).
>
> If upstream changes, **update this file in the same PR** that updates
> the adapter. Do not let drift creep in.

---

## 1. Scope & Coverage

Per Groww docs verbatim:

> **Note:** Currently, Backtesting APIs only support CASH and FNO segments.
> Data for Equities, Indices, and FNO instruments are available from
> **2020 onwards**.

Key implications for BruteX:

| Fact | BruteX implication |
|---|---|
| CASH + FNO only | COMMODITY segment out of scope — matches `REQUIREMENTS.md §1.3` |
| FNO data from 2020 | Fixed 5-year+ lookback, matches `FD-08` |
| Equities from 2020 | **Equities also limited to 2020** (earlier I assumed deeper history — correction below) |
| Indices from 2020 | Same |

**Fact-check:** Earlier I assumed NSE equities might have deeper history than
2020 via Groww. **That was wrong.** The docs explicitly state all three
categories start at 2020. `REQUIREMENTS.md §1.2` horizon is correct as-is.

---

## 2. Groww Symbol Format (verbatim from docs)

> Groww symbol is a unique identifier for an instrument across exchanges
> and segments. Constructed by concatenating:
>
> - **Exchange** — NSE, BSE
> - **Trading Symbol** — the name/ticker
> - **Expiry Date** — only for derivatives, format `DDMmmYY` (e.g., `23Jan25`)
> - **Strike Price** — only for options
> - **Option Type** — `CE` (Call), `PE` (Put), `FUT` (Futures)

### Canonical examples

```
Equity      NSE-WIPRO
            BSE-RELIANCE
Index       NSE-NIFTY
            BSE-SENSEX
Future      NSE-NIFTY-30Sep25-FUT
            BSE-SENSEX-25Sep25-FUT
Call Option NSE-NIFTY-30Sep25-24650-CE
            BSE-SENSEX-25Sep25-79500-CE
Put Option  NSE-NIFTY-30Sep25-24650-PE
            BSE-SENSEX-25Sep25-79500-PE
```

### Parsing rules BruteX must implement

| Token | Position | Format | Rule |
|---|---|---|---|
| Exchange | 1st | `NSE` \| `BSE` | literal |
| Underlying | 2nd | string | same as `underlying_symbol` field |
| Expiry (derivatives only) | 3rd | `DDMmmYY` | 2-digit day + 3-letter month + 2-digit year, **no separator** inside the token |
| Strike (options only) | 4th | integer or decimal | rupees |
| Side (derivatives only) | last | `CE` \| `PE` \| `FUT` | — |
| Separator | — | `-` (hyphen) | between all tokens |

### ⚠️ Source-of-truth rule

Even though the `groww_symbol` string is **parseable**, BruteX's Groww
adapter MUST NOT rely on parsing it. The authoritative source for
`instrument_type`, `expiry_date`, `strike_price`, `underlying_symbol`
is the instrument CSV row (see `docs/groww-sdk/instruments.md §4`).
Parse-as-fallback-only, and compare the parsed values against the CSV
fields in an integrity gate. Any mismatch → `SymbolFormatDriftError`.

---

## 3. `get_expiries` API

Retrieves available expiry dates for derivatives (FNO) for a given
exchange and underlying symbol.

### Python SDK usage (verbatim)

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

expiries_response = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=1
)
print(expiries_response)
```

### Signature

```python
groww.get_expiries(
    exchange: str,              # required — use EXCHANGE_NSE / EXCHANGE_BSE
    underlying_symbol: str,     # required — e.g., "NIFTY", "BANKNIFTY", "RELIANCE"
    year: int = None,           # optional, 2020-current, defaults to current year
    month: int = None,          # optional, 1-12, if omitted returns entire year
) -> dict
```

### Request schema

| Name | Type | Required | Description |
|---|---|---|---|
| `exchange` | string | YES | Stock exchange (NSE, BSE) |
| `underlying_symbol` | string | YES | Underlying symbol |
| `year` | integer | NO | 2020 – current. Defaults to current year. |
| `month` | integer | NO | 1–12. If omitted, returns entire year. |

### Response

```json
{
  "expiries": [
    "2024-01-25",
    "2024-01-31",
    "2024-02-29",
    "2024-03-28"
  ]
}
```

| Name | Type | Description |
|---|---|---|
| `expiries` | `array[string]` | Expiry dates in `YYYY-MM-DD` format |

### 🎯 BruteX-critical fact

**`year` accepts 2020 onwards — so historical expiries from 2020 onwards
are enumerable.**

This resolves the single biggest open question I had about the Groww
adapter. Earlier drafts of `REQUIREMENTS.md §8.3` described a risky
"deterministic symbol probe" approach to recover pre-snapshot expired
options. **That approach is now deleted** — we use `get_expiries` with
explicit `year=2020..2026` to enumerate the entire historical expiry
set for each underlying.

---

## 4. `get_contracts` API

Retrieves the set of groww symbols for all contracts (strikes × sides
+ the future) at a specific expiry.

### Python SDK usage (verbatim)

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

contracts_response = groww.get_contracts(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    expiry_date="2025-01-25"
)
print(contracts_response)
```

### Signature

```python
groww.get_contracts(
    exchange: str,              # required
    underlying_symbol: str,     # required (1–20 chars)
    expiry_date: str,           # required, "YYYY-MM-DD"
) -> dict
```

### Response

```json
{
  "contracts": [
    "NSE-NIFTY-02Jan25-28500-PE",
    "NSE-NIFTY-02Jan25-24000-PE",
    "NSE-NIFTY-02Jan25-26800-PE",
    "NSE-NIFTY-02Jan25-27450-PE",
    "NSE-NIFTY-02Jan25-19050-PE",
    "NSE-NIFTY-02Jan25-22300-PE",
    "NSE-NIFTY-02Jan25-28150-CE"
  ]
}
```

| Name | Type | Description |
|---|---|---|
| `contracts` | `array[string]` | Groww symbols for all contracts at that expiry |

### ⚠️ Key observation

`get_contracts` returns **bare symbol strings**, not structured objects.
To get `strike_price`, `option_type`, `lot_size`, `tick_size`, etc. for
each contract, BruteX must either:

1. **Parse the `groww_symbol`** per §2 above (quick, fragile)
2. **Look up each symbol in the instrument CSV** via `get_instrument_by_groww_symbol`
   (authoritative, slower) or via a pre-loaded DataFrame lookup (authoritative,
   fast after one-time CSV load)

**BruteX adopts path 2** for correctness and drift-safety. The instrument
CSV (`get_all_instruments()`) is loaded once per ingest run into an
in-memory pandas / polars DataFrame indexed by `groww_symbol`. Contract
metadata is looked up in O(1) during the candle-fetching loop.

---

## 5. `get_historical_candles` API

Fetches historical OHLCV(+OI) candle data.

### What it provides

- **O, H, L, C** for all instruments
- **Volume** for tradable instruments (equities and FNO) — **NOT** for
  indices in CASH segment (see §8 important notes)
- **Open Interest (OI)** for FNO only — `null` for everything else

### Python SDK usage — Equity / Index

```python
from growwapi import GrowwAPI

groww = GrowwAPI("your_token")

historical_candles_response = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_CASH,
    groww_symbol="NSE-WIPRO",
    start_time="2025-09-24 10:56:00",
    end_time="2025-09-24 12:00:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_30,
)
print(historical_candles_response)
```

### Python SDK usage — FNO (the full workflow)

```python
# Step 1: enumerate expiries
jan2024_nifty_expiries = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=1,
)

# Step 2: enumerate contracts for one expiry
nifty_24_jan_contracts = groww.get_contracts(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    expiry_date=jan2024_nifty_expiries['expiries'][0],
)

# Step 3: fetch candles for one contract
candles = groww.get_historical_candles(
    exchange=groww.EXCHANGE_NSE,
    segment=groww.SEGMENT_FNO,
    groww_symbol=nifty_24_jan_contracts['contracts'][0],
    start_time="2024-01-01 09:15:00",
    end_time="2024-01-10 15:30:00",
    candle_interval=groww.CANDLE_INTERVAL_MIN_15,
)
```

### Signature

```python
groww.get_historical_candles(
    exchange: str,           # required — NSE / BSE
    segment: str,            # required — CASH / FNO
    groww_symbol: str,       # required — canonical ID from instrument CSV
    start_time: str | int,   # required — "yyyy-MM-dd HH:mm:ss" OR epoch seconds
    end_time: str | int,     # required — same formats as start_time
    candle_interval: str,    # required — one of the 12 CANDLE_INTERVAL_* constants
) -> dict
```

### Request schema

| Name | Type | Required | Description |
|---|---|---|---|
| `exchange` | string | YES | `NSE` or `BSE` |
| `segment` | string | YES | `CASH` or `FNO` |
| `groww_symbol` | string | YES | Canonical Groww identifier |
| `start_time` | string \| int | YES | `"yyyy-MM-dd HH:mm:ss"` or epoch seconds |
| `end_time` | string \| int | YES | same formats |
| `candle_interval` | string | YES | see `docs/groww-sdk/annexures.md §4` |

### Response (verbatim)

```json
{
  "candles": [
    ["2025-09-24T10:30:00", 245.95, 246.15, 245.05, 245.6, 735060, null],
    ["2025-09-24T11:00:00", 245.64, 245.66, 244.8, 244.94, 682373, null]
  ],
  "closing_price": 245.40,
  "start_time": "2025-09-24 10:30:00",
  "end_time": "2025-09-24 12:00:00",
  "interval_in_minutes": 30
}
```

### Candle array layout — positional, not keyed

Each bar in `response["candles"]` is a **flat array**, not a dict:

| Index | Meaning | Type | Notes |
|---:|---|---|---|
| 0 | timestamp | string | `yyyy-MM-ddTHH:mm:ss` (ISO 8601, **T separator**) |
| 1 | open | float | rupees |
| 2 | high | float | rupees |
| 3 | low | float | rupees |
| 4 | close | float | rupees |
| 5 | volume | int | `null` for indices in CASH segment |
| 6 | open interest | int | `null` for everything except FNO |

### ⚠️ Input vs output timestamp format is DIFFERENT

- **Input** (`start_time`, `end_time`): `"yyyy-MM-dd HH:mm:ss"` (space separator)
- **Output** (inside each candle array): `"yyyy-MM-ddTHH:mm:ss"` (T separator, ISO 8601)

BruteX's adapter MUST normalise both to a single internal epoch-microsecond
representation (`ts_open_utc: int64`) before they hit the data lake.
Never let these raw string formats leak above L1.

### Response schema

| Name | Type | Description |
|---|---|---|
| `candles` | `array[array]` | List of positional candle arrays |
| `closing_price` | float | Closing price at end_time |
| `start_time` | string | `yyyy-MM-dd HH:mm:ss` |
| `end_time` | string | `yyyy-MM-dd HH:mm:ss` |
| `interval_in_minutes` | int | Interval converted to minutes |

---

## 6. Backtesting Data Limits — THE CRITICAL TABLE

Maximum **time range** per single `get_historical_candles` request:

| Candle interval | Max duration per request |
|---|---|
| 1 min, 2 min, 3 min, 5 min | **30 days** |
| 10 min, 15 min, 30 min | **90 days** |
| 1 hour, 4 hours, 1 day, 1 week, 1 month | **180 days** |

### 🎯 This changes BruteX's call budget math completely

Earlier drafts of `REQUIREMENTS.md §8` assumed "max bars per call" was
the binding constraint and used ~1000 bars as a conservative guess.
**That assumption is wrong.** The real constraint is **time-based**,
and it's much more generous.

| Interval | Bars per max-duration call (trading hours only) |
|---|---|
| 1 min | 30 days × 22 days/mo × 375 bars/day = **~8,250 bars/call** |
| 5 min | 30 days × 22 × 75 = **~1,650 bars/call** |
| 15 min | 90 days × 22/mo × 25 bars/day = **~4,950 bars/call** |
| 30 min | 90 days × 22/mo × 12.5 bars/day = **~2,475 bars/call** |
| 1 hour | 180 days × 22/mo × 6 bars/day = **~2,376 bars/call** |
| 1 day | 180 days × 22/mo / day = **~132 bars/call** |

### BruteX request chunker

The adapter must implement a chunker that splits requested ranges into
the max-duration windows above. This is required by `FR-ING-006`.

```python
MAX_DURATION_DAYS = {
    "MIN_1": 30,  "MIN_2": 30,  "MIN_3": 30,  "MIN_5": 30,
    "MIN_10": 90, "MIN_15": 90, "MIN_30": 90,
    "HOUR_1": 180, "HOUR_4": 180,
    "DAY": 180, "WEEK": 180, "MONTH": 180,   # note: no _1 suffix on DAY/WEEK/MONTH — matches Groww SDK
}

def chunk_request(start: datetime, end: datetime, interval: str):
    max_days = MAX_DURATION_DAYS[interval]
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=max_days), end)
        yield (cursor, chunk_end)
        cursor = chunk_end
```

---

## 7. Typical Backtesting Workflow (verbatim from docs)

```python
from growwapi import GrowwAPI

groww = GrowwAPI("your_token")

# 1. Get available expiries for a month
expiries = groww.get_expiries(
    exchange=groww.EXCHANGE_NSE,
    underlying_symbol="NIFTY",
    year=2024,
    month=6,
)

# 2. For each expiry, get available contracts
for expiry in expiries['expiries']:
    contracts = groww.get_contracts(
        exchange=groww.EXCHANGE_NSE,
        underlying_symbol="NIFTY",
        expiry_date=expiry,
    )

    # 3. For each contract, fetch historical candles
    for contract in contracts['contracts']:
        candles = groww.get_historical_candles(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_FNO,
            groww_symbol=contract,
            start_time="2024-06-01 09:15:00",
            end_time="2024-06-30 15:30:00",
            candle_interval=groww.CANDLE_INTERVAL_MIN_5,
        )
        # Process candles for backtesting...
```

This is **exactly** the algorithm BruteX Phase P1c will implement, with
these additions on top:

1. Token-bucket rate limiter wrapping every call
2. Exponential backoff retry on 429/5xx
3. Content-addressed raw response storage before parsing
4. Idempotent dedup via `sha256(request_params)` — re-running skips done chunks
5. Integrity gates (gap/NaN/dup/monotonic) after each parse
6. Audit-chain entry per request (start + complete + error)

---

## 8. Important notes (verbatim from docs)

> - For requests exceeding max duration limits, **paginate by splitting
>   into multiple requests**
> - **FNO data starts from 2020 only**
> - **Open Interest is only available for FNO instruments** (null for CASH)
> - **All prices are in Indian Rupees (INR)**
> - **Timestamps are in IST (Indian Standard Time)**
> - **Volume is not available for index instruments in CASH segment**

### BruteX implications

| Note | BruteX action |
|---|---|
| Pagination beyond max duration | Chunker in §6 — mandatory |
| FNO from 2020 | `FD-08` horizon is correct |
| OI null for CASH | Store as `None` in `Bar.oi`, never confuse with `0` |
| INR | `Decimal` type everywhere for money |
| **IST timestamps** | Adapter MUST convert to UTC microseconds on ingest |
| Volume null for indices in CASH | Integrity gate G-ZERO-VOL must tolerate this for `instrument_type=IDX` + `segment=CASH` |

---

## 9. Constant Naming — RESOLVED

Earlier drafts of this file flagged a potential discrepancy between two
doc pastes where one version of the backtesting page appeared to use
`CANDLE_INTERVAL_DAY_1` / `WEEK_1` / `MONTH_1` (with `_1` suffix) while
the annexures page used the bare `CANDLE_INTERVAL_DAY` / `WEEK` / `MONTH`
(no suffix).

After comparing a second, more comprehensive paste from the product owner:

- **Annexures page (both pastes):** bare names, no suffix
- **Backtesting page code examples (latest paste):** uses
  `CANDLE_INTERVAL_MIN_15`, `CANDLE_INTERVAL_MIN_30` — no day/week/month
  examples shown, so no contradiction with annexures
- **Earlier paste's appendix** had `_1` variants, but this looks like an
  older or annotated version

**Canonical names** (matches `docs/groww-sdk/annexures.md §4`):

```python
groww.CANDLE_INTERVAL_MIN_1
groww.CANDLE_INTERVAL_MIN_2
groww.CANDLE_INTERVAL_MIN_3
groww.CANDLE_INTERVAL_MIN_5
groww.CANDLE_INTERVAL_MIN_10
groww.CANDLE_INTERVAL_MIN_15
groww.CANDLE_INTERVAL_MIN_30
groww.CANDLE_INTERVAL_HOUR_1
groww.CANDLE_INTERVAL_HOUR_4
groww.CANDLE_INTERVAL_DAY        # ← no _1 suffix
groww.CANDLE_INTERVAL_WEEK       # ← no _1 suffix
groww.CANDLE_INTERVAL_MONTH      # ← no _1 suffix
```

**Safety net:** the golden-master test
`tests/golden/test_groww_sdk_constants_frozen` introspects
`dir(growwapi.GrowwAPI)` at CI time and asserts the exact set of
`CANDLE_INTERVAL_*` attributes. If Groww ever renames any of them
upstream, CI fails loudly and the adapter is updated deliberately
(never silently). See `docs/groww-sdk/annexures.md §13`.

---

## 10. BruteX P1c Algorithm — Locked

With all three APIs now documented, the P1c candle-ingest algorithm is:

```python
# brutex/adapters/groww/ingest.py (illustrative)

from datetime import date, datetime, timedelta
from growwapi import GrowwAPI

V1_UNDERLYINGS = [
    ("NSE", "NIFTY"),
    ("NSE", "BANKNIFTY"),
    ("NSE", "FINNIFTY"),
    ("NSE", "MIDCPNIFTY"),
    ("NSE", "NIFTYNXT50"),
    ("BSE", "SENSEX"),
    ("BSE", "BANKEX"),
    ("BSE", "SENSEX50"),
]

HORIZON_START = date(2020, 1, 1)
HORIZON_END   = date.today()

def ingest_full_fno_universe(groww: GrowwAPI, interval: str):
    instruments_df = groww.get_all_instruments()  # load once, O(1) lookups
    instruments_by_symbol = instruments_df.set_index("groww_symbol")

    for exchange, underlying in V1_UNDERLYINGS:

        # (a) enumerate every historical expiry across the horizon
        for year in range(HORIZON_START.year, HORIZON_END.year + 1):
            resp = rate_limited(groww.get_expiries)(
                exchange=exchange, underlying_symbol=underlying, year=year,
            )
            expiries = resp["expiries"]  # ["2024-01-04", ...]

            # (b) for each expiry, enumerate every contract
            for expiry in expiries:
                resp = rate_limited(groww.get_contracts)(
                    exchange=exchange,
                    underlying_symbol=underlying,
                    expiry_date=expiry,
                )
                contracts = resp["contracts"]

                # (c) also fetch the spot underlying (IDX) and the future (FUT)
                # from the CSV for the same expiry window

                # (d) fetch candles per contract, chunked by the max-duration table
                for groww_symbol in contracts:
                    meta = instruments_by_symbol.loc[groww_symbol]
                    first_trade_ts = derive_first_trade_ts(meta, expiry)
                    for chunk_start, chunk_end in chunk_request(
                        start=first_trade_ts, end=expiry_plus_one_day(expiry),
                        interval=interval,
                    ):
                        fetch_and_persist(
                            groww,
                            exchange=exchange,
                            segment=groww.SEGMENT_FNO,
                            groww_symbol=groww_symbol,
                            start_time=chunk_start,
                            end_time=chunk_end,
                            candle_interval=interval,
                        )
```

Every call goes through:

1. **Token bucket** (≤15 req/s, per `FR-ING-007`)
2. **Content-addressed dedup** — `sha256(exchange||segment||symbol||interval||start||end)` — skip if already fetched
3. **Retry with backoff** on 429/5xx
4. **Raw-blob persistence** before parsing
5. **Integrity gates** (G-GAP/DUP/NAN/MONO) after parsing
6. **Audit-chain entry** for start, complete, and any error

---

## 11. Recalculated Call Budget for v1 (honest, based on the real limits)

### v1 universe (locked)

8 F&O indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX, SENSEX50

### Call count for 1-minute bars (the heaviest pass)

The binding constraint is **30 days per call**. Most option contracts live
for 1–30 days, so the typical option needs **1 call**. Spot indices and
futures that run across the full horizon need ~60 calls each (5 years / 30 days).

| Component | Contracts | Calls per contract | Total calls |
|---|---:|---:|---:|
| Spot indices (IDX) | 8 | 60 | 480 |
| Index futures (~180 each × 8) | 1,440 | ~3 | 4,320 |
| Index options (~207,000 over 5y) | 207,000 | ~1 | 207,000 |
| **Plus** `get_expiries` enum calls | 8 underlyings × 5 years | | ~40 |
| **Plus** `get_contracts` calls | ~2,500 distinct expiries | | ~2,500 |
| **Total** | | | **~214,340 calls** |

### Wall-clock at 15 req/s effective (BruteX safety cap)

`214,340 / 15 ≈ 14,290 seconds ≈ 3.97 hours`

### For each additional interval (5m, 15m, 1h, 1d)

Same ~214k calls (options still need only 1 call each at higher intervals
because they live < 30 days, which fits every interval's budget). So
each additional interval = **~4 more hours**.

### Total for the "all timeframes" pass

| Intervals | Wall-clock |
|---|---|
| 1m only | ~4 hours |
| 1m + 5m + 15m + 1h + 1d (5 intervals) | ~20 hours (overnight + morning) |
| 1m + all 12 native intervals | ~48 hours (one weekend) |

### vs my old (wrong) estimate

| Version | 1-min full v1 universe | Source of error |
|---|---|---|
| Original "all equities" plan | **~12.7 days** | Scope too large (all NSE) |
| "NSE F&O indices only" first draft | ~15.7 hours | 1000-bar/call guess (wrong) |
| **Corrected with real 30-day limit** | **~4 hours** | ✅ this is the actual number |

**95%+ faster than my original estimate.** The full v1 universe ingest
fits in a single morning, not a week.

---

## 12. REQUIREMENTS.md sections this file obsoletes / updates

| REQUIREMENTS.md section | Action |
|---|---|
| §8 Ingest Profile | **REWRITE** with 30/90/180-day limits and ~4-hour wall-clock |
| §8.3 "Pre-2026 expired option recovery" | **DELETE** — `get_expiries` handles this natively |
| §4.4 FR-UNI-004 "deterministic symbol probe" | **DELETE** — same reason |
| §26 OQ-01..04 (max bars, rate limit, retry-after) | **PARTIALLY RESOLVED** — max bars known, rate limit TBD in P1c empirically |
| §26 add `OQ-GROWW-CANDLE-NAMES` | NEW — `_1` suffix discrepancy |
| §2 HC-12 "Max bars per call undocumented" | **UPDATE** to: "Max duration per call: 30/90/180 days by interval group" |

All of these amendments will land in the same PR as this file.

---

## 13. Changelog

| Date | Change | Commit |
|---|---|---|
| 2026-04-14 | Initial capture of full backtesting page | `(this commit)` |

---

_End of `docs/groww-sdk/backtesting.md`_

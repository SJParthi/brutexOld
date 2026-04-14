# Groww SDK — Instruments Reference

> **Source:** https://groww.in/trade-api/docs/python-sdk/instruments
> **Captured:** 2026-04-14 (pasted verbatim by product owner)
> **SDK package:** `growwapi` (PyPI)
> **Purpose:** Authoritative project-local reference for the Groww Python SDK
> instrument API. BruteX's adapter (`brutex.adapters.groww.instruments`)
> MUST match the signatures documented here.
>
> If the upstream docs change, **update this file in the same PR** that updates
> the adapter. Do not let drift creep in.

---

## 1. What is an "instrument" in Groww?

From Groww docs:

> Instruments are the financial assets that can be traded on exchanges through
> the Groww API. These include stocks, futures, options, commodities, indices
> and other tradable securities.

Each distinct tradable entity — a single equity, a single futures contract, a
single option contract (one strike × one side × one expiry) — is one instrument
with its own unique `groww_symbol` and `exchange_token`.

---

## 2. CSV download URL

```
https://growwapi-assets.groww.in/instruments/instrument.csv
```

Also available in the SDK as:

```python
groww.INSTRUMENT_CSV_URL
```

The CSV is a daily snapshot — it reflects the universe as of the most recent
refresh. **Expired contracts are not in today's CSV.** To reconstruct historical
universes, we must snapshot daily going forward (see `REQUIREMENTS.md §4.4`
and `§8.3`).

---

## 3. SDK methods (verbatim from Groww docs)

### 3.1 `get_instrument_by_groww_symbol`

```python
from growwapi import GrowwAPI

API_AUTH_TOKEN = "your_token"
groww = GrowwAPI(API_AUTH_TOKEN)

get_instrument_by_groww_symbol_response = groww.get_instrument_by_groww_symbol(
    groww_symbol="NSE-RELIANCE"
)
print(get_instrument_by_groww_symbol_response)
```

**Signature (inferred from example):**
```python
groww.get_instrument_by_groww_symbol(groww_symbol: str) -> dict
```

**Returns:** a dict with the 19 instrument fields (see §4 below).

### 3.2 `get_instrument_by_exchange_and_trading_symbol`

```python
get_instrument_by_trading_symbol_response = groww.get_instrument_by_exchange_and_trading_symbol(
    exchange=groww.EXCHANGE_NSE,
    trading_symbol="RELIANCE",
)
print(get_instrument_by_trading_symbol_response)
```

**Signature:**
```python
groww.get_instrument_by_exchange_and_trading_symbol(
    exchange: str,          # use groww.EXCHANGE_NSE / EXCHANGE_BSE constants
    trading_symbol: str,
) -> dict
```

### 3.3 `get_instrument_by_exchange_token`

```python
get_instrument_by_exchange_token_response = groww.get_instrument_by_exchange_token(
    exchange_token="2885"
)
print(get_instrument_by_exchange_token_response)
```

**Signature:**
```python
groww.get_instrument_by_exchange_token(exchange_token: str) -> dict
```

### 3.4 `get_all_instruments`

```python
instruments_df = groww.get_all_instruments()
print(instruments_df.head())
```

**Signature:**
```python
groww.get_all_instruments() -> pandas.DataFrame
```

**Returns:** a pandas DataFrame with one row per instrument, columns as per §4.

### 3.5 `_load_instruments` (internal / advanced)

```python
instrument_df = groww._load_instruments()
# OR
instrument_df = pd.read_csv(groww.INSTRUMENT_CSV_URL)
```

Internal loader. Both calls return the same DataFrame. Prefer
`get_all_instruments()` in production code; use `_load_instruments()` only
when you need identical caching behaviour to the SDK internals.

---

## 4. Instrument fields — the 19 canonical columns

Captured verbatim from the "Instrument CSV Columns" section of the Groww docs.

| # | Name | Type | Description |
|---|------|------|-------------|
| 1 | `exchange` | string | The exchange where the instrument is traded |
| 2 | `exchange_token` | string | The unique token assigned to the instrument by the exchange |
| 3 | `trading_symbol` | string | The trading symbol of the instrument to place orders with |
| 4 | `groww_symbol` | string | The symbol used by Groww to identify the instrument |
| 5 | `name` | string | The name of the instrument |
| 6 | `instrument_type` | string | The type of the instrument (EQ, CE, PE, FUT, INDEX, …) |
| 7 | `segment` | string | Segment such as CASH, FNO, etc. |
| 8 | `series` | string | The series of the instrument (e.g., EQ, A, B, etc.) |
| 9 | `isin` | string | The ISIN (International Securities Identification Number) |
| 10 | `underlying_symbol` | string | Symbol of the underlying asset (for derivatives). Empty for stocks and indices. |
| 11 | `underlying_exchange_token` | string | The exchange token of the underlying asset |
| 12 | `lot_size` | number | The minimum lot size for trading the instrument |
| 13 | `expiry_date` | string | The expiry date of the instrument (for derivatives) |
| 14 | `strike_price` | number | The strike price of the instrument (for options) |
| 15 | `tick_size` | number | The minimum price movement for the instrument |
| 16 | `freeze_quantity` | number | The quantity that is frozen for trading |
| 17 | `is_reserved` | boolean | Whether the instrument is reserved for trading |
| 18 | `buy_allowed` | boolean | Whether buying the instrument is allowed |
| 19 | `sell_allowed` | boolean | Whether selling the instrument is allowed |

> **⚠️ Fact-check:** Earlier drafts of `CLAUDE.md` and `REQUIREMENTS.md` said
> "21 columns". **That was wrong.** The authoritative count is **19**. Same
> commit that adds this file also corrects the count in those two files.

---

## 5. Example output — equity (NSE-RELIANCE)

```python
{
    'exchange': 'NSE',
    'exchange_token': 2885,
    'trading_symbol': 'RELIANCE',
    'groww_symbol': 'NSE-RELIANCE',
    'name': 'Reliance Industries',
    'instrument_type': 'EQ',
    'segment': 'CASH',
    'series': 'EQ',
    'isin': 'INE002A01018',
    'underlying_symbol': nan,
    'underlying_exchange_token': nan,
    'lot_size': 1,
    'expiry_date': nan,
    'strike_price': nan,
    'tick_size': 5,
    'freeze_quantity': nan,
    'is_reserved': 0,
    'buy_allowed': 1,
    'sell_allowed': 1,
}
```

Notes:

- Equity rows have `segment=CASH`, `instrument_type=EQ`.
- Derivative-only fields (`underlying_symbol`, `underlying_exchange_token`,
  `expiry_date`, `strike_price`, `freeze_quantity`) are `nan` for equities.
- `tick_size` is in paise (5 = ₹0.05).

---

## 6. Example output — derivatives (observed in `get_all_instruments().head()`)

From the Groww docs screenshot of `get_all_instruments()`:

| row | exchange | exchange_token | trading_symbol | groww_symbol |
|----:|---------|---------------:|----------------|--------------|
| 0 | NSE | 35154 | `ASIANPAINT25FEBFUT` | `NSE-ASIANPAINT-Feb25-FUT` |
| 1 | NSE | 35158 | `ABB25APR9600PE` | `NSE-ABB-Apr25-9600-PE` |
| 2 | NSE | 35044 | `NIFTY25MAR29050PE` | `NSE-NIFTY-27Mar25-29050-PE` |
| 3 | NSE | 35093 | `FINNIFTY25MAR29600PE` | `NSE-FINNIFTY-Mar25-29600-PE` |
| 4 | NSE | 35047 | `NIFTYNXT5025FEB56600CE` | `NSE-NIFTYNXT50-Feb25-56600-CE` |

From a BANKNIFTY-focused view:

| exchange_token | trading_symbol | groww_symbol |
|---:|---|---|
| 49445 | `BANKNIFTY25DEC27700PE` | `NSE-BANKNIFTY-24Dec25-27700-PE` |
| 60123 | `BANKNIFTY25DEC51000CE` | `NSE-BANKNIFTY-24Dec25-51000-CE` |
| 60183 | `BANKNIFTY25DEC48500PE` | `NSE-BANKNIFTY-24Dec25-48500-PE` |
| 42279 | `BANKNIFTY25SEP28500CE` | `NSE-BANKNIFTY-25Sep25-28500-CE` |
| 61719 | `BANKNIFTY25SEP39000CE` | `NSE-BANKNIFTY-25Sep25-39000-CE` |
| 60099 | `BANKNIFTY25DEC37500PE` | `NSE-BANKNIFTY-24Dec25-37500-PE` |
| 41942 | `BANKNIFTY25JUN35400PE` | `NSE-BANKNIFTY-26Jun25-35400-PE` |
| 73176 | `BANKINDIA25JUL96CE` | `NSE-BANKINDIA-31Jul25-96-CE` |

---

## 7. Observed `groww_symbol` format patterns

These are patterns **observed in actual SDK output**, not documented contracts.
BruteX must not rely on them as stable — parse defensively and fall back to the
dict fields (`instrument_type`, `expiry_date`, `strike_price`, `underlying_symbol`)
as the source of truth.

| Kind | Example `groww_symbol` | Template (observed) |
|------|------------------------|---------------------|
| Equity | `NSE-RELIANCE` | `<EXCH>-<TSYM>` |
| Stock future | `NSE-ASIANPAINT-Feb25-FUT` | `<EXCH>-<UNDERLYING>-<MonYY>-FUT` |
| Stock option (monthly) | `NSE-ABB-Apr25-9600-PE` | `<EXCH>-<UNDERLYING>-<MonYY>-<STRIKE>-<SIDE>` |
| Stock option (weekly, full date) | `NSE-BANKINDIA-31Jul25-96-CE` | `<EXCH>-<UNDERLYING>-<DDMonYY>-<STRIKE>-<SIDE>` |
| Index option (weekly, full date) | `NSE-NIFTY-27Mar25-29050-PE` | `<EXCH>-<UNDERLYING>-<DDMonYY>-<STRIKE>-<SIDE>` |
| Index option (weekly, BANKNIFTY) | `NSE-BANKNIFTY-24Dec25-27700-PE` | `<EXCH>-<UNDERLYING>-<DDMonYY>-<STRIKE>-<SIDE>` |
| Index option (monthly, FINNIFTY) | `NSE-FINNIFTY-Mar25-29600-PE` | `<EXCH>-<UNDERLYING>-<MonYY>-<STRIKE>-<SIDE>` |
| Index option (monthly, NIFTYNXT50) | `NSE-NIFTYNXT50-Feb25-56600-CE` | `<EXCH>-<UNDERLYING>-<MonYY>-<STRIKE>-<SIDE>` |

### Important observation

Both `trading_symbol` **and** `groww_symbol` are carried in every row.
`trading_symbol` is what you send to the **order API** (live). `groww_symbol`
is the canonical ID used by the **historical / SDK lookups**. They are **not
interchangeable** — BruteX's adapter must store both and index by both.

---

## 8. SDK constants used by the adapter

From the examples in the Groww docs:

| Constant | Used for |
|---|---|
| `groww.EXCHANGE_NSE` | `exchange=` parameter, NSE |
| `groww.EXCHANGE_BSE` | (inferred) `exchange=` parameter, BSE |
| `groww.INSTRUMENT_CSV_URL` | Direct CSV URL for the instrument snapshot |

Full list of exchange / segment / interval constants is on the **Annexures**
page (not yet captured). Capture in `docs/groww-sdk/annexures.md` when
the product owner pastes it.

---

## 9. What this file does NOT cover

The following belong on **sibling files** in `docs/groww-sdk/`:

| File (to be created) | Source page | Contents |
|---|---|---|
| `docs/groww-sdk/backtesting.md` | `python-sdk/backtesting` | `get_expiries`, `get_contracts`, `get_historical_candles`, `backtesting-data-limits` |
| `docs/groww-sdk/historical-data.md` | `python-sdk/historical-data` | live historical endpoints (if different from backtesting) |
| `docs/groww-sdk/annexures.md` | `python-sdk/annexures` | exchange / segment / interval / instrument_type enum constants |

**These are NOT yet captured.** Product owner must paste them before the
adapter can be fully specified.

---

## 10. Mapping to BruteX internals

When BruteX ingests an instrument row, it normalises to:

```python
@dataclass(frozen=True)
class Instrument:
    exchange:          Exchange       # NSE | BSE
    segment:           Segment        # CASH | FNO
    instrument_type:   InstrumentType # EQ | INDEX | FUT | CE | PE
    groww_symbol:      str            # authoritative SDK ID — O(1) lookup key
    trading_symbol:    str            # order API identifier
    exchange_token:    int
    name:              str
    isin:              str | None
    underlying_symbol: str | None     # for FUT/CE/PE
    expiry_date:       date | None    # for FUT/CE/PE
    strike_price:      Decimal | None # for CE/PE
    lot_size:          int
    tick_size:         Decimal        # stored in rupees, not paise
    freeze_quantity:   int | None
    series:            str | None     # EQ, A, B, …
    buy_allowed:       bool
    sell_allowed:      bool
    is_reserved:       bool
    # ──────────────────────────────────────────
    # Derived fields (not in Groww CSV):
    first_seen_date:   date           # from BruteX temporal master
    last_seen_date:    date
    snapshot_hashes:   frozenset[str] # content-addressed provenance
```

All Groww rows flow through this normaliser. Downstream layers (L2 data lake,
L3 virtual feed, L4 exec sim, L5 strategy engine) only see this normalised
form — they never import `growwapi` directly.

This is how BruteX stays swappable to other brokers later (Zerodha, IBKR,
Binance) without touching strategy or indicator code.

---

## 11. Integrity checks on every ingest

Before a row is accepted into the temporal master, the adapter MUST verify:

| Check | Why |
|---|---|
| `groww_symbol` is unique in the DataFrame | Primary key assertion |
| `exchange` is one of `{NSE, BSE, MCX}` | Catch future exchange additions early |
| `segment` is one of `{CASH, FNO, COMMODITY}` | Catch future segment additions early |
| `instrument_type` is one of `{EQ, INDEX, FUT, CE, PE}` | Catch new types |
| If `instrument_type ∈ {FUT, CE, PE}`: `underlying_symbol` is non-empty | Sanity |
| If `instrument_type ∈ {CE, PE}`: `strike_price > 0` | Sanity |
| If `instrument_type ∈ {FUT, CE, PE}`: `expiry_date` is a parseable date | Sanity |
| `lot_size > 0` | Sanity |
| `tick_size > 0` | Sanity |

Any failure → row is rejected and logged to `data/audit/instrument_rejects.jsonl`.
Never silently dropped.

---

## 12. Observed inconsistencies — real, unresolved

Found while reviewing a second doc paste from the product owner. None of these
are deal-breakers, but the adapter MUST handle them.

### 12.1 `internal_trading_symbol` — the 20th column (undocumented)

The official "Instrument CSV Columns" table in §4 lists **19 columns**. But the
actual output of `get_all_instruments().head()` shows an additional
`internal_trading_symbol` column:

```
  exchange exchange_token          trading_symbol                   groww_symbol  ... is_reserved buy_allowed sell_allowed internal_trading_symbol
0      NSE          35154      ASIANPAINT25FEBFUT       NSE-ASIANPAINT-Feb25-FUT  ...           0           1            1      ASIANPAINT25FEBFUT
1      NSE          35158          ABB25APR9600PE          NSE-ABB-Apr25-9600-PE  ...           1           1            1          ABB25APR9600PE
```

So the actual DataFrame has **20 columns**, not 19. In the examples seen,
`internal_trading_symbol` appears to be a duplicate of `trading_symbol`, but
this is **unverified** and may diverge for some instruments.

**Adapter behaviour:**

1. Read all 20 columns from the DataFrame
2. Check in a CI golden-master test that `internal_trading_symbol == trading_symbol`
   for every row
3. If the assertion ever fails, log the diff and mark the row for review
4. Store `internal_trading_symbol` in the raw parquet but not in the canonical
   `Instrument` dataclass — only surfaces if the assertion breaks
5. **Do NOT** rely on `internal_trading_symbol` as a primary key — use
   `groww_symbol` as documented

This is tracked as `OQ-GROWW-INTERNAL-TRADING-SYMBOL` in `REQUIREMENTS.md §26`.

### 12.2 `tick_size` format inconsistency — paise vs rupees

Two different sample outputs disagree on the `tick_size` format:

**From the CSV raw row (text representation):**
```
tick_size  0.05
```
Float in rupees — 0.05 means ₹0.05 (5 paise).

**From `get_instrument_by_groww_symbol("NSE-RELIANCE")` dict:**
```python
{'...', 'tick_size': 5, '...'}
```
Integer — 5 what? Almost certainly **paise** (= ₹0.05), matching the CSV
float value. But could also be a unit confusion.

**Adapter behaviour:**

1. Normalise **both forms** to a canonical representation:
   `Decimal("0.05")` in rupees (never `float`, never `int`)
2. Detection logic:
   - If value is a float with at most 3 decimal places → treat as rupees
   - If value is an integer ≥ 1 and < 1000 → treat as paise, divide by 100
   - Otherwise → raise `TickSizeFormatError` with the raw value
3. Cross-validate: after normalisation, assert `tick_size ∈ {0.01, 0.05, 0.10, 0.25, 0.50, 1.00, 5.00}` (the common NSE/BSE tick sizes)
4. Golden-master test records the normalised value for every liquid contract

Tracked as `OQ-GROWW-TICK-SIZE-UNIT` in `REQUIREMENTS.md §26`.

### 12.3 `lot_size` is temporal — it changes across expiries

The real CSV sample shows lot size drift for BANKNIFTY:

| groww_symbol | expiry_date | lot_size |
|---|---|---:|
| NSE-BANKNIFTY-26Jun25-35400-PE | 2025-06-26 | **30** |
| NSE-BANKNIFTY-25Sep25-28500-CE | 2025-09-25 | **35** |
| NSE-BANKNIFTY-24Dec25-27000-PE | 2025-12-24 | **35** |

BANKNIFTY's lot size changed from 30 to 35 between June 2025 and September
2025. This is a real SEBI-mandated lot-size revision and it means:

1. **The temporal master MUST track `lot_size` per expiry** (not per
   underlying). Already implied by `instruments_temporal` in
   `REQUIREMENTS.md §6.2`, but this real example makes it concrete.
2. **Backtest exec sim** must look up the lot size via
   `instruments_temporal.as_of(trade_date).lot_size` — never cache the
   "current" lot size and apply it to historical trades.
3. **Golden test:** a BANKNIFTY trade dated 2025-07-15 must size using
   `lot_size=30`, and the same trade re-run dated 2025-10-15 must size
   using `lot_size=35`. Both values must persist in the temporal master.

`freeze_quantity` likely follows the same temporal pattern (SEBI revises
both together). Assume temporal until proven otherwise.

This refines `FR-UNI-002` in `REQUIREMENTS.md` and adds
`MG-TEMPORAL-LOT-SIZE` to the missing-fn register (actually accepted, not
missing — just making the requirement explicit).

### 12.4 `freeze_quantity` is per-contract, not per-underlying

Same sample shows:

| groww_symbol | freeze_quantity |
|---|---:|
| NSE-BANKNIFTY-* (all Dec 2025) | 601 |
| NSE-BANKNIFTY-* (all Jun 2025) | 601 |
| NSE-BANKINDIA-31Jul25-96-CE | **208001** |

BANKINDIA is a stock option with a completely different freeze quantity
(208001 vs BANKNIFTY's 601). So `freeze_quantity` is per-contract, not
per-underlying, and must be stored on every row.

Adapter already does this via the `Instrument` dataclass in §10, so no
change — just making it explicit.

---

## 13. Changelog for this reference file

| Date | Change | Commit |
|---|---|---|
| 2026-04-14 | Initial capture from Groww docs (19 documented columns) | `4793353` |
| 2026-04-14 | Add §12: undocumented 20th column, tick_size format inconsistency, BANKNIFTY lot_size temporal variance, per-contract freeze_quantity | `(this commit)` |

---

_End of `docs/groww-sdk/instruments.md`_

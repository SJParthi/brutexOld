# Groww SDK — Annexures (Enum Constants)

> **Source:** https://groww.in/trade-api/docs/python-sdk/annexures
> **Captured:** 2026-04-14 (pasted verbatim by product owner)
> **SDK package:** `growwapi` (PyPI) — imported as `from growwapi import GrowwAPI`
>
> **Purpose:** Authoritative project-local reference for every constant the
> Groww Python SDK exposes via the `GrowwAPI` class. BruteX's Groww adapter
> MUST use these SDK constants — never hardcode string literals like `"CASH"`
> or `"1minute"` in adapter code.
>
> If upstream changes, **update this file in the same PR** that updates
> the adapter.

---

## 1. Why this file exists

The Groww SDK uses several fixed enum-like parameters for trading operations.
Hardcoding their string values in our adapter would:

1. Break silently if Groww renames `"1minute"` → `"1m"` tomorrow
2. Make test golden-masters fragile
3. Drift between live (tickvault) and backtest (brutex) code

**The rule:** import the constant from `GrowwAPI`, never the literal.

```python
# ❌ WRONG — fragile, fails on upstream rename
candles = groww.get_historical_candles(..., candle_interval="1minute")

# ✅ RIGHT — survives upstream renames
candles = groww.get_historical_candles(..., candle_interval=GrowwAPI.CANDLE_INTERVAL_MIN_1)
```

---

## 2. Exchange

Used in every method that takes an `exchange` parameter.

| SDK Constant | Value | Description |
|---|---|---|
| `GrowwAPI.EXCHANGE_BSE` | `BSE` | Bombay Stock Exchange — Asia's oldest exchange, known for SENSEX |
| `GrowwAPI.EXCHANGE_NSE` | `NSE` | National Stock Exchange — India's largest by volume |
| `GrowwAPI.EXCHANGE_MCX` | `MCX` | Multi Commodity Exchange — commodity derivatives |

**BruteX scope:** `NSE` + `BSE` (MCX deferred — commodity segment is not
supported by Groww's backtesting endpoint per docs).

---

## 3. Segment

Used with `exchange` to select the trading segment.

| SDK Constant | Value | Description |
|---|---|---|
| `GrowwAPI.SEGMENT_CASH` | `CASH` | Regular equity market for trading stocks with delivery option |
| `GrowwAPI.SEGMENT_FNO` | `FNO` | Futures and Options segment for trading derivatives contracts |
| `GrowwAPI.SEGMENT_COMMODITY` | `COMMODITY` | Commodity derivatives on MCX |

**BruteX scope:** `FNO` (v1 is F&O indices only; CASH deferred to v1.1
when individual equities are added).

**⚠️ Unverified:** Earlier research indicated Groww's **backtesting**
endpoint supports only `CASH` + `FNO` (no `COMMODITY`). Confirm when
product owner pastes `docs/python-sdk/backtesting`.

---

## 4. Candle Interval — CRITICAL for P1c

Every historical-candle call requires one of these constants.

**All 12 verified intervals:**

| SDK Constant | Value | Description |
|---|---|---|
| `GrowwAPI.CANDLE_INTERVAL_MIN_1` | `1minute` | 1 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_2` | `2minute` | 2 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_3` | `3minute` | 3 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_5` | `5minute` | 5 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_10` | `10minute` | 10 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_15` | `15minute` | 15 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_MIN_30` | `30minute` | 30 minute interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_1` | `1hour` | 1 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_HOUR_4` | `4hour` | 4 hour interval |
| `GrowwAPI.CANDLE_INTERVAL_DAY` | `1day` | 1 day interval |
| `GrowwAPI.CANDLE_INTERVAL_WEEK` | `1week` | 1 week interval |
| `GrowwAPI.CANDLE_INTERVAL_MONTH` | `1month` | 1 month interval |

### Observations for BruteX

- **Native base bar:** 1-minute is the finest bar Groww exposes. BruteX's
  data lake stores 1-minute as the **canonical base**; everything else is
  resampled on demand.
- **3m and 2m are native** — we don't have to resample those, we can pull
  them directly if needed. Useful for strategies that prefer 2m or 3m
  windows without the resample cost.
- **Pre-aggregated 4h, 1day, 1week, 1month** — for very coarse backtests,
  we can pull these directly instead of resampling 1-minute, saving both
  API calls and storage. BruteX's resolver SHOULD prefer native intervals
  when available.
- **No sub-minute (no tick, no seconds bars)** — this is the physical
  fidelity ceiling of BruteX v1. Documented in `REQUIREMENTS.md §2 HC-04`.

---

## 5. Instrument Type — **correction from earlier drafts**

Used in `instrument.instrument_type` field from the CSV / SDK dicts.

| Value | Description |
|---|---|
| `EQ` | Equity — Represents ownership in a company |
| **`IDX`** | **Index — Composite value of a group of stocks representing a market** |
| `FUT` | Futures — Derivatives contract to buy/sell an asset at a future date |
| `CE` | Call Option — Derivatives contract giving the right to buy an asset |
| `PE` | Put Option — Derivatives contract giving the right to sell an asset |

### ⚠️ FACT-CHECK CORRECTION

Earlier drafts of `REQUIREMENTS.md §4.5` and `§6.1` used `INDEX` for the
index instrument type. **That was wrong.** The Groww SDK canonical value is
**`IDX`** (3 characters). Same commit that adds this file corrects
`REQUIREMENTS.md`.

### BruteX internal mapping

```python
class InstrumentType(str, Enum):
    EQ  = "EQ"    # equity
    IDX = "IDX"   # index (NIFTY, BANKNIFTY, SENSEX, ...)
    FUT = "FUT"   # futures (stock or index)
    CE  = "CE"    # call option
    PE  = "PE"    # put option
```

Any other value in a Groww row is rejected by the integrity gate with
`UnknownInstrumentTypeError` — never silently accepted.

---

## 6. Order Status (reference only — v1 doesn't place orders)

| Value | Description |
|---|---|
| `NEW` | Order is newly created and pending for further processing |
| `ACKED` | Order has been acknowledged by the system |
| `TRIGGER_PENDING` | Order is waiting for a trigger event to be executed |
| `APPROVED` | Order has been approved and is ready for execution |
| `REJECTED` | Order has been rejected by the system |
| `FAILED` | Order execution has failed |
| `EXECUTED` | Order has been successfully executed |
| `DELIVERY_AWAITED` | Order has been executed and waiting for delivery |
| `CANCELLED` | Order has been cancelled |
| `CANCELLATION_REQUESTED` | Request to cancel the order has been initiated |
| `MODIFICATION_REQUESTED` | Request to modify the order has been initiated |
| `COMPLETED` | Order has been completed |

**BruteX:** out of scope — BruteX does not place orders. Captured here only
so the internal exec simulator can mirror these status transitions for
golden-master tests against real order lifecycle logs from tickvault.

---

## 7. After Market Order Status

(Not pasted by product owner yet. Fill when available.)

**BruteX:** out of scope for v1.

---

## 8. Order Type

Partially captured from screenshot:

| SDK Constant | Value | Description |
|---|---|---|
| `GrowwAPI.ORDER_TYPE_LIMIT` | `LIMIT` | Specify exact price, may not get filled immediately but ensures price control |
| _(others pending paste)_ | | |

**BruteX:** out of scope for v1. Useful as reference for the exec simulator
to mirror real order types when backtesting against tickvault golden masters.

---

## 9. Product

Not pasted. Fill when available.

**BruteX:** out of scope for v1.

---

## 10. Transaction Type

Not pasted. Fill when available.

**BruteX:** out of scope for v1.

---

## 11. Validity

| SDK Constant | Value | Description |
|---|---|---|
| `GrowwAPI.VALIDITY_DAY` | `DAY` | Valid until market close on the same trading day |
| _(if others exist, paste them)_ | | |

**BruteX:** out of scope for v1.

---

## 12. BruteX adapter wiring — how each constant is used

```python
# brutex/adapters/groww/client.py  (illustrative, not yet written)

from growwapi import GrowwAPI
from brutex.models import Exchange, Segment, Interval, InstrumentType

SDK_EXCHANGE = {
    Exchange.NSE: GrowwAPI.EXCHANGE_NSE,
    Exchange.BSE: GrowwAPI.EXCHANGE_BSE,
}

SDK_SEGMENT = {
    Segment.CASH: GrowwAPI.SEGMENT_CASH,
    Segment.FNO:  GrowwAPI.SEGMENT_FNO,
}

SDK_INTERVAL = {
    Interval.MIN_1:   GrowwAPI.CANDLE_INTERVAL_MIN_1,
    Interval.MIN_2:   GrowwAPI.CANDLE_INTERVAL_MIN_2,
    Interval.MIN_3:   GrowwAPI.CANDLE_INTERVAL_MIN_3,
    Interval.MIN_5:   GrowwAPI.CANDLE_INTERVAL_MIN_5,
    Interval.MIN_10:  GrowwAPI.CANDLE_INTERVAL_MIN_10,
    Interval.MIN_15:  GrowwAPI.CANDLE_INTERVAL_MIN_15,
    Interval.MIN_30:  GrowwAPI.CANDLE_INTERVAL_MIN_30,
    Interval.HOUR_1:  GrowwAPI.CANDLE_INTERVAL_HOUR_1,
    Interval.HOUR_4:  GrowwAPI.CANDLE_INTERVAL_HOUR_4,
    Interval.DAY:     GrowwAPI.CANDLE_INTERVAL_DAY,
    Interval.WEEK:    GrowwAPI.CANDLE_INTERVAL_WEEK,
    Interval.MONTH:   GrowwAPI.CANDLE_INTERVAL_MONTH,
}

SDK_INSTRUMENT_TYPE = {
    "EQ":  InstrumentType.EQ,
    "IDX": InstrumentType.IDX,
    "FUT": InstrumentType.FUT,
    "CE":  InstrumentType.CE,
    "PE":  InstrumentType.PE,
}
```

The adapter is the **only** place these mappings live. Every other
layer sees only BruteX's internal enums.

---

## 13. Golden-master test (required for P1c)

```python
# tests/golden/test_groww_sdk_constants.py

from growwapi import GrowwAPI

EXPECTED = {
    "EXCHANGE_NSE": "NSE",
    "EXCHANGE_BSE": "BSE",
    "EXCHANGE_MCX": "MCX",
    "SEGMENT_CASH": "CASH",
    "SEGMENT_FNO": "FNO",
    "SEGMENT_COMMODITY": "COMMODITY",
    "CANDLE_INTERVAL_MIN_1":  "1minute",
    "CANDLE_INTERVAL_MIN_2":  "2minute",
    "CANDLE_INTERVAL_MIN_3":  "3minute",
    "CANDLE_INTERVAL_MIN_5":  "5minute",
    "CANDLE_INTERVAL_MIN_10": "10minute",
    "CANDLE_INTERVAL_MIN_15": "15minute",
    "CANDLE_INTERVAL_MIN_30": "30minute",
    "CANDLE_INTERVAL_HOUR_1": "1hour",
    "CANDLE_INTERVAL_HOUR_4": "4hour",
    "CANDLE_INTERVAL_DAY":    "1day",
    "CANDLE_INTERVAL_WEEK":   "1week",
    "CANDLE_INTERVAL_MONTH":  "1month",
    "VALIDITY_DAY":           "DAY",
}

def test_groww_sdk_constants_frozen():
    """
    Golden-master test. If Groww renames ANY of these constants upstream,
    this test fails loudly and forces us to update our adapter deliberately
    instead of silently breaking.
    """
    for name, expected_value in EXPECTED.items():
        actual = getattr(GrowwAPI, name)
        assert actual == expected_value, (
            f"Groww SDK constant drift: GrowwAPI.{name} = {actual!r}, "
            f"expected {expected_value!r}. Update docs/groww-sdk/annexures.md "
            f"and the adapter mapping tables before merging."
        )
```

This is `CG-01`'s line-of-defense against upstream drift.

---

## 14. What's NOT yet captured

The product owner must still paste the following pages before P1a can be
fully specified:

| Page | URL | Why needed |
|---|---|---|
| **Backtesting** | `https://groww.in/trade-api/docs/python-sdk/backtesting` | `get_expiries`, `get_contracts`, `get_historical_candles` signatures + rate limits + max bars per call |
| **Historical Data** | `https://groww.in/trade-api/docs/python-sdk/historical-data` | If it differs from backtesting endpoint |
| **After Market Order Status**, **Order Type** (full), **Product**, **Transaction Type** | annexures page (scroll up/down) | Out of scope for v1 but nice to capture |

---

## 15. Changelog for this reference file

| Date | Change | Commit |
|---|---|---|
| 2026-04-14 | Initial capture from Groww docs (Exchange, Segment, Candle Interval × 12, Instrument Type × 5, Validity, Order Status × 12) | `(this commit)` |

---

_End of `docs/groww-sdk/annexures.md`_

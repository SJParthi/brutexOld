# Track 1 — Indian Market Microstructure Handling

> **Status:** Locked 2026-05-15 by Agent 3 (Hostile Review).
> Companion to `TRACK1_BACKTEST_SPEC.md §17`.

This document specifies how Track 1 handles the edge cases of Indian
equity-index trading that the prior spec missed.

---

## 1. Trading Session

### 1.1 Standard session (NSE + BSE equity)

```
09:00 - 09:15 IST  →  Pre-open auction (call auction). EXCLUDED.
09:15 - 15:30 IST  →  Continuous trading. INCLUDED.
15:30 - 15:40 IST  →  Closing call auction. EXCLUDED.
```

**Spot index ticks** start at **09:15:00 IST** (post-auction opening
value). Bars before this are phantom synthetic values that some
vendors expose during 09:08-09:15. These MUST be filtered.

### 1.2 Forced exit (Track 1 convention)

```
15:20:00 IST — all open positions force-closed
```

This is a Track 1 simulator convention (gives a 10-minute exit margin
before close). Property P-09 enforces no trade survives 15:20.

---

## 2. Half-Day Sessions

Some holidays have half-day sessions. The exact hours are exchange-
defined per event. Track 1 handles them via the calendar:

| Event | Typical hours | Calendar source |
|---|---|---|
| Diwali Muhurat (yearly) | 1 hour; varies by Samvat | hardcoded list (see §3 below) |
| Ram Navami (when on trading day) | 09:15-12:30 or similar | NSE calendar |
| Eid (when on trading day) | half session | NSE calendar |

Half-day session detection: calendar lookup returns `(open_time,
close_time)` per trading day. If close < 15:30, that day is a half-day
and the **15:20 forced exit** is replaced by `close_time - 10 min`.

---

## 3. Muhurat Trading Sessions (Diwali, yearly)

| Year | Samvat | Date | Hours (IST) | Notes |
|---|---|---|---|---|
| 2021 | 2078 | 2021-11-04 | 18:15 - 19:15 | Evening |
| 2022 | 2079 | 2022-10-24 | 18:15 - 19:15 | Evening |
| 2023 | 2080 | 2023-11-12 | 18:15 - 19:15 | Evening |
| 2024 | 2081 | 2024-11-01 | 18:15 - 19:15 | Evening |
| 2025 | 2082 | 2025-10-21 | 13:45 - 14:45 | **CHANGED** to afternoon |
| 2026 | 2083 | TBD | TBD | Confirm at year start |

**Treatment:** Muhurat bars are EXCLUDED from Track 1 backtests by
hardcoded date list. They are anomalous: low volume, ceremonial, no
follow-through next session. Including them would pollute results.

The exclusion list lives at
`brutex/microstructure/muhurat_dates.py` (Phase 2 deliverable):

```python
MUHURAT_EXCLUSION_DATES: frozenset[date] = frozenset({
    date(2021, 11, 4),
    date(2022, 10, 24),
    date(2023, 11, 12),
    date(2024, 11, 1),
    date(2025, 10, 21),
    # 2026 added when confirmed
})
```

---

## 4. NSE/BSE Holiday Calendar 2021-2026

Loaded from a TOML file (`brutex/microstructure/calendar_2021_2026.toml`)
generated from the official NSE/BSE PDFs. Property P-22 asserts no
bar timestamp falls on a holiday.

| Year | Holidays | Source |
|---|---|---|
| 2021 | 18 | NSE official |
| 2022 | 16 | NSE official |
| 2023 | 15 | NSE official |
| 2024 | 14 | NSE official |
| 2025 | 14 | NSE official |
| 2026 | TBD (partial year) | NSE official |

The calendar fuzz target (`cargo fuzz` target #4) tests the TOML
parser against malformed inputs.

---

## 5. NIFTY 50 Reconstitution

NIFTY 50 is reconstituted semi-annually:

- **March 31** (effective from April first trading day after F&O
  expiry)
- **September 30** (effective from October first trading day after F&O
  expiry)

In the 2021-05-15 → 2026-05-15 window, there are **~10 reconstitution
events**. The index series Groww publishes is **chain-linked** through
these — the level is continuous, no artificial gap from composition
change.

**Treatment:**

- Each reconstitution date is flagged in the audit log
- The data series passes through (no special handling on the level)
- If future Track 1 versions add breadth or sector conditions
  (currently NOT in vocabulary), those would need composition-aware
  computation and the analyst would need to use the
  point-in-time composition. Track 1's 52 atomic conditions are all
  price-action based, so no leak.

---

## 6. SENSEX 30 Reconstitution

BSE SENSEX is reconstituted on a less rigid schedule (typically June
and December). Same treatment as NIFTY: chain-linked series, dates
flagged, no composition-aware conditions in Track 1.

---

## 7. Circuit Breaker Halts

NSE/BSE have market-wide and instrument-wide circuit breakers.

### 7.1 Market-wide (rare; NIFTY 50 ±10%, ±15%, ±20%)

- March 2020 (BEFORE Track 1 window of 2021-05-15+) — known event
- April 2025 (April 7) — 10% breaker, trading halted 45 min — IN
  window

**Treatment:** Drop the entire trading day from the dataset. Property
P-22 asserts no bar timestamp on excluded days.

Halt detection: any bar where the daily range > 9% triggers a manual
review at ingest. Specific halt dates are added to the calendar
exclusion list.

### 7.2 Instrument-wide (intraday)

NIFTY 50 spot does NOT trade — it's calculated. Component stocks may
hit circuit but the index will still publish a tick. So instrument
circuit breakers don't directly affect Track 1 data, but the source
candle may show an unusually flat 1-min bar where most constituents
halted. This is rare and acceptable — Track 1 strategies are
price-action based and a flat bar simply doesn't fire any conditions.

---

## 8. Gap-Up / Gap-Down Opening

The opening 09:15:00 bar reflects the auction-cleared price, which can
gap significantly from the prior close (overnight news, global market
moves, etc.).

**Definitions:**

- `gap_up_day`: `open[09:15] > prev_close × 1.005` (≥ 0.5% gap up)
- `gap_down_day`: `open[09:15] < prev_close × 0.995` (≥ 0.5% gap down)

Both are vocabulary conditions (bits 48, 49). Strategies can opt to
fire ONLY on gap days, ONLY on non-gap days, or ignore.

---

## 9. Inside / Outside Day Detection

- `inside_day`: `day_high < pdh AND day_low > pdl` (so far this session)
- `outside_day`: `day_high > pdh AND day_low < pdl` (so far this session)

Both are vocabulary conditions (bits 50, 51). Note these are **running
intraday** — at 09:30 a day might look "inside" but become "outside"
by 11:00. Bars are evaluated at their bar close, so a strategy that
requires `inside_day` will fire on bar X if `inside_day` is true at
bar X's close, even if it later becomes false. **No look-ahead.**

---

## 10. Previous-Trading-Day Anchoring

`pdh` and `pdl` are anchored to the **previous trading day**, not
calendar `date - 1`. The calendar lookup handles weekends and holiday
clusters.

**Example:**
- Monday 2024-09-23 → previous trading day = Friday 2024-09-20
- Monday after Holi week 2024-03-25 → previous trading day = Friday
  2024-03-22 (Holi 25-26 are holidays, but the data feed continues
  normally on 25th if there's market — verify calendar)

This is enforced by computing PDH/PDL from the daily-bars table
(fetched via `POST /v2/charts/historical`), where holidays are simply
absent from the daily series.

---

## 11. Time-of-Day Window Boundaries

The 4 time-of-day windows (vocabulary bits 44-47):

| Window | Start (IST) | End (IST) | Bars (5m) |
|---|---|---|---|
| Early morning | 09:16:00 | 10:00:00 | 9 bars |
| Mid morning | 10:00:00 | 11:30:00 | 18 bars |
| Midday | 11:30:00 | 13:00:00 | 18 bars |
| Afternoon | 13:00:00 | 15:00:00 | 24 bars |

Window membership = `start_ist ≤ bar_close_ist < end_ist`.

Why these boundaries:

- Early morning captures opening-range setups
- Mid-morning captures sustained early trend
- Midday captures lunch-hour reversals
- Afternoon captures end-of-day push (excluded last 30 min already
  via 15:20 forced exit)

A strategy may pick at most ONE time window (mutually exclusive within
group 8).

---

## 12. Verification

Property tests enforce these microstructure constraints:

| Invariant | Enforces |
|---|---|
| P-22 | No bar timestamp on any NSE holiday 2021-2026 |
| P-23 | Every persisted intraday bar has 09:15 ≤ bar_time < 15:30 IST |
| P-18 | Groww rate limiter timing |

Golden corpora (`tests/golden/fixtures/`) include specific edge-case
dates: Diwali Muhurat, Ram Navami half-session, Holi Monday, election
holiday. The conversion of these days from Groww response → canonical
Parquet must match the frozen SHA256.

---

## 13. Sources

- NSE official trading calendar: nseindia.com
- BSE official trading calendar: bseindia.com
- Kotak Securities Diwali Muhurat 2025 timing change reference
- NSE Reconstitution Schedule (semi-annual): niftyindices.com
- Market circuit breaker reference: nseindia.com circulars

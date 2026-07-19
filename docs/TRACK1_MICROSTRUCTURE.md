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

## 4. NSE/BSE Holiday Calendar 2020-2026

Hardcoded verified date lists in `brutex/calendars/nse.py`
(`ALL_HOLIDAYS`, split per exchange), sourced from the official NSE/BSE
circulars. Property P-22 asserts no bar timestamp falls on a holiday.

| Year | Holidays | Source |
|---|---|---|
| 2020 | 12 | NSE official (DEC-CALENDAR-2020-DATA-001; 2 Nov dates tagged UNVERIFIED in-source) |
| 2021 | 18 | NSE official |
| 2022 | 16 | NSE official |
| 2023 | 15 | NSE official |
| 2024 | 14 | NSE official |
| 2025 | 14 | NSE official |
| 2026 | TBD (partial year) | NSE official |

(The original design loaded a `calendar_2021_2026.toml` parsed at
runtime with a fuzz target; the shipped calendar is the hardcoded,
test-pinned Python list above — no TOML parser exists.)

---

## 5. NIFTY 50 Reconstitution

NIFTY 50 is reconstituted semi-annually:

- **March 31** (effective from April first trading day after F&O
  expiry)
- **September 30** (effective from October first trading day after F&O
  expiry)

In the rolling 2020-01-01 → today window (the data-pull floor
`GROWW_HISTORY_FLOOR`, DEC-PULL-FLOOR-2020-001, MIGRATION.md #722),
there are **~2 reconstitution events per year**. The index series Groww
publishes is **chain-linked** through these — the level is continuous,
no artificial gap from composition change.

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

The market-wide breaker framework is SEBI Circular
SMDRPD/Policy/Cir-37/2001 (2001-06-28) as modified by CIR/MRD/DP/25/2013
(2013-09-03 — dynamic bands off the previous close, triggered by BSE
Sensex or Nifty 50, whichever breaches first); canonical exchange page
nseindia.com/products-services/equity-market-circuit-breakers (verified
2026-07-11).

- 2020-03-13 — 10% lower-band halt at 09:20 IST (Nifty −10.07% to
  8,625); 45-min halt + 15-min pre-open call auction; continuous trading
  resumed 10:20 IST; **normal 15:30 close** (Sensex closed +1,325 at
  34,103) — IN window (pull floor 2020-01-01, MIGRATION.md #722);
  annotated in `brutex/calendars/nse.py` `CIRCUIT_HALT_SESSIONS`
  (verified 2026-07-11:
  https://www.business-standard.com/article/markets/nifty-hits-lower-circuit-amid-global-sell-off-trading-halted-for-45-mins-120031300188_1.html
  · https://www.businesstoday.in/markets/stocks/story/sensex-nifty-crash-circuit-breaker-rules-252013-2020-03-13).
- 2020-03-23 — 10% lower-band halt at 09:58 IST; resumed after the
  standard 45-min-halt + 15-min-pre-open sequence (~10:58 IST — derived
  from that sequence, the literal minute is not press-printed); **normal
  15:30 close** (Sensex −13.15%, Nifty 7,610.25 — the worst-ever
  single-day fall, recorded at the regular close) — annotated (verified
  2026-07-11:
  https://www.business-standard.com/article/markets/sensex-hits-10-lower-circuit-trading-halted-for-45-minutes-120032300237_1.html
  · https://www.businesstoday.in/markets/market-perspective/story/sensex-hits-circuit-breaker-of-10-trading-halted-on-bse-nse-252823-2020-03-23).
- April 7, 2025 was a large gap-down day (~−5% intraday worst, normal
  15:30 close, Nifty closed 22,161.60, −3.24%) — the earlier claim in
  this document of a market-wide breaker halt that day is **REFUTED**
  (verified 2026-07-11:
  https://groww.in/blog/sensex-and-nifty-live-updates-today-april-7-2025
  · https://www.business-standard.com/markets/news/market-crash-what-are-circuit-filters-for-the-sensex-and-nifty-explained-125040700243_1.html);
  the market-wide breaker has not triggered since March 2020. Unknown
  future halt days are discovered by the `prebuild.daily_range_extreme`
  detector, never asserted.

**Treatment (DEC-HALT-DAY-TREATMENT-001): ANNOTATE, never drop.** Stored
bars are never deleted (house law, DEC-LAKE-HEAL-001 — strictly
row-removing heals only, never value-editing; the halt-day bars are
real, well-formed prints). Known halt dates live in
`brutex/calendars/nse.py` `CIRCUIT_HALT_SESSIONS`; the session-density
census (`brutex/data/session_density.py`) reads them as
expected-irregular (INFO, never WARN) and any un-annotated session with
(high − low) / low ≥ 9% raises `prebuild.daily_range_extreme` for manual
review at the pipeline prebuild — if a future halt is confirmed by an
NSE/SEBI circular, its date is ADDED to `CIRCUIT_HALT_SESSIONS` with the
citation. P-22 remains holidays-only.

### 7.2 Instrument-wide (intraday)

NIFTY 50 spot does NOT trade — it's calculated. Component stocks may
hit circuit but the index will still publish a tick. So instrument
circuit breakers don't directly affect Track 1 data, but the source
candle may show an unusually flat 1-min bar where most constituents
halted. This is rare and acceptable — Track 1 strategies are
price-action based and a flat bar simply doesn't fire any conditions.

### 7.3 2021-02-24 NSE exchange outage (extended session)

NOT a circuit breaker — a telecom/SDN infrastructure failure
(DEC-EXTENDED-SESSION-OUTAGE-001). Verified timeline (2026-07-11, SEBI
press releases:
https://www.sebi.gov.in/media/press-releases/mar-2021/trading-halt-at-nse-on-february-24-2021_49603.html
· https://www.sebi.gov.in/media/press-releases/feb-2021/trading-halt-at-nse-on-february-24-2021_49287.html
· https://taxguru.in/sebi/sebi-clarifies-trading-halt-nse-february-24-2021.html
· https://www.moneylife.in/article/nse-trading-halt-rbi-says-failure-at-nse-clearing-led-to-outage-on-24th-february/63276.html):

- NSE data feeds stopped updating at **10:08 IST**; NSE halted **F&O at
  11:40** and the **cash market at 11:43** citing issues with the links
  with telecom service providers.
- NSE notified the market at **15:17** that trading would resume; a
  **15-minute pre-open call auction ran 15:30–15:45**, and **continuous
  trading resumed at 15:45** (not 15:30 — the corrected timeline).
- **Trading hours were extended to 17:00 IST at NSE, BSE AND MSEI.** BSE
  functioned normally all day and traded continuously 09:15–17:00 (so
  SENSEX has a full extended day), while NSE indices have an interior
  ~11:40 → 15:45 dead zone with no bars.

**Treatment:** modeled as `brutex/calendars/nse.py` `EXTENDED_SESSIONS`
(the session HOURS — one (09:15, 17:00) pair, which captures BSE exactly
and NSE's envelope) + the session-density census annotation (the NSE
midday hole is a DATA fact, deliberately not calendar-modeled). Real
15:30–16:59 bars on this date are ADMITTED by the calendar-aware ingest
session filter; the forced exit that day is **16:50 IST** (the session
close − 10 min law of `forced_exit_time_for_session` — the same rule
Muhurat sessions already follow), a sanctioned single-date
engine-visible delta pinned by fails-before trade-sim tests.

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

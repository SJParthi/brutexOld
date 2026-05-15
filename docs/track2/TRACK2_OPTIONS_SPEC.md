# BruteX Track 2 — Options Track Master Spec

> **Status:** Phase 0 design (Sprint 1 of 1).
> **Locked:** 2026-05-15 evening (this PR).
> **Inherits from:** `docs/TRACK1_BACKTEST_SPEC.md`. Read that first.

_Last updated: 2026-05-15._

---

## 0. Purpose

Track 2 is the **execution-realism leg** of BruteX:

1. Track 1 finds spot-index signals (signal-only research; no fills, no costs).
2. Track 2 takes a Track 1 winning signal and translates it into a
   **strike-aware, premium-realistic** options trade so we can measure
   the **post-cost, post-slippage P&L** the trader would actually
   experience.

Track 2 is **NOT** a separate brute-force engine over options
combinations. The 52-condition vocabulary stays in Track 1.

Track 2's job is:
- Ingest historical 1-minute options OHLC for the ATM ± 10 strike band
  around NIFTY / BANKNIFTY / SENSEX.
- Compute Greeks at every bar (IV from Dhan; delta/gamma/theta/vega
  from Black-Scholes-Merton).
- For a Track 1 signal: simulate the options trade with realistic
  brokerage / STT / STO / slippage, returning a P&L vector that
  reflects what a live trader would see.

---

## 1. Scope (locked)

| In scope | Out of scope |
|---|---|
| Indian index options (NIFTY 50, BANKNIFTY, SENSEX) — Dhan `OPTIDX` | All single-stock options (FNO equity) |
| Weekly + monthly expiry cycles (per-underlying, see §6) | LEAPS, quarterly long-dated, exotic structures |
| ATM ± 10 strike band (Dhan `/charts/rollingoption` enforces this) | Deeper OTM than ATM ± 10 |
| 1-minute bars only | Any other timeframe (no resampling) |
| Greeks: IV (from Dhan) + delta/gamma/theta/vega (computed) | Vanna, charm, color, higher-order Greeks |
| Single-leg long premium (buy CE or buy PE) | Multi-leg structures (straddles, condors, calendars) for first sweep |
| Brokerage / STT / STO / SEBI / GST cost model | Margin / portfolio-level risk |
| Slippage as a parameter sweep | Live order placement |

**India VIX is NOT in Track 2.** Dhan does not list options on India VIX
(NSE doesn't either — VIX is a calculated index, not a tradable
contract). VIX remains reference-only via Track 1's `vix_at_entry` /
`vix_at_exit` stamps.

---

## 2. Universe (locked)

### Underlyings — Dhan facts (verified 2026-05-15)

| Underlying | Spot secId | Spot exchange/segment | Options exchange/segment | Trading symbol |
|---|---|---|---|---|
| NIFTY 50 | `13` | `IDX_I` / `INDEX` | `NSE_FNO` / `OPTIDX` | `NIFTY` |
| BANKNIFTY | `25` | `IDX_I` / `INDEX` | `NSE_FNO` / `OPTIDX` | `BANKNIFTY` |
| SENSEX | `51` | `IDX_I` / `INDEX` | `BSE_FNO` / `OPTIDX` | `SENSEX` |

Hardcoded in `brutex/adapters/dhan/options_constants.py` (Phase 2 deliverable).

### Strike radius (locked at 21 strikes)

For every (underlying, expiry, day):

| Slot | Meaning |
|---|---|
| `ATM-10` | 10 strikes below ATM |
| `ATM-9` … `ATM-1` | progressively closer to ATM |
| `ATM` | at-the-money strike |
| `ATM+1` … `ATM+10` | progressively further OTM (CE) / further ITM (PE), flip for puts |
| Total | **21 strikes** × {CE, PE} = **42 series** per (underlying, expiry, day) |

Dhan's `/charts/rollingoption` API enforces `strikePosition ∈ [-10, +10]`.
No silent expansion beyond that.

### Expiry cycle history — 5y backfill window 2021-01-01 → 2026-05-15

The historical window spans multiple SEBI/NSE/BSE regime changes.
**Backfill must respect per-regime date gating** — a single "current
day-of-week" mapping is wrong for the 5y window.

#### NIFTY 50 (NSE_FNO)
| Period | Weekly day | Monthly day | Source |
|---|---|---|---|
| 2021-01-01 → 2025-08-28 | Thursday | Last Thursday | NSE original 2019-02-11 launch (circ. 39675/39704); FAOP/60011 |
| 2025-09-02 → today | **Tuesday** | Last Tuesday | NSE FAOP/68747; SEBI standardization circular May 2025; first Tuesday weekly Sep 2, 2025 |

> **Withdrawn regime annotation (Mar-Apr 2025):** NSE announced an
> all-weekly shift to **Monday** effective 2025-04-04 (circular early
> March 2025), then **withdrew it on 2025-03-27** (Zerodha bulletin
> 407122, Business Standard) before the first Monday expiry could trade.
> NIFTY weekly stayed Thursday through 2025-08-28 and jumped directly to
> Tuesday 2025-09-02. **The Monday window never traded** and is NOT a
> backfill regime — do not split watermarks on these dates.

#### BANKNIFTY (NSE_FNO)
| Period | Weekly day | Monthly day | Source |
|---|---|---|---|
| 2021-01-01 → 2023-09-03 | Thursday | Last Thursday | NSE original 2016-05-27 launch (BS/PTI 2016-05-05) |
| 2023-09-04 → 2024-02-29 | **Wednesday** | Last Thursday | NSE circ. 119/2023 = FAOP57540 (2023-07-12); first Wed weekly 2023-09-06 |
| 2024-03-01 → 2024-11-13 | **Wednesday** | **Last Wednesday** | NSE FAOP/60011 (2023-12-29); first Wed monthly 2024-03-27 |
| 2024-11-14 → 2024-12-31 | ❌ **KILLED** | Last Wednesday | NSE FAOP/64506 (2024-10-10) implementing SEBI master circular 2024-10-01; last BN weekly Wed 2024-11-13 (Zeebiz "ends today" 2024-11-13); 2 monthly expiries on Last-Wed: 2024-11-27, 2024-12-24 |
| 2025-01-01 → 2025-08-28 | ❌ **KILLED** | **Last Thursday** | NSE FAOP/65336 (eff. EOD 2025-01-01) reverted monthly Wed→Thu; first Last-Thu monthly 2025-01-30; Business Standard 2024-11-30 confirms |
| 2025-09-02 → today | ❌ **KILLED** | **Last Tuesday** | NSE FAOP/68747 + SEBI standardization May 2025; first Last-Tue monthly 2025-09-30 |

#### SENSEX (BSE_FNO)
| Period | Weekly day | Monthly day | Source |
|---|---|---|---|
| 2021-01-01 → 2023-05-12 | **Thursday** (thin liquidity) | Last Thursday | **BSE Notice 20230327-65** (verbatim: "All existing contracts of S&P BSE SENSEX (weekly, monthly and long dated) … with expiry day as Thursday expired on May 12, 2023 EOD"). BSE FNO segment was illiquid pre-relaunch; contracts existed and were tradable but volume was near-zero (~₹crores/day vs NSE ₹lakh-crores/day) |
| 2023-05-15 → 2024-12-31 | Friday | Last Friday | BSE Notice 20230327-65 (cycle revision); Business Standard 2023-05-15 confirms first Fri weekly 2023-05-19. No gap from prior Thu cycle (last Thu = 2023-05-12; first Fri = 2023-05-19) |
| 2025-01-01 → 2025-08-31 | Tuesday | Last Tuesday | Business Standard 2024-10-04; 5paisa Jan-2025 article. **Carve-out: 2025-01-03 SENSEX weekly kept Friday as one-off transition** (do not flag as Tuesday in expiry calendar) |
| 2025-09-04 → today | **Thursday** | Last Thursday | SEBI standardization May 2025; first Thursday SENSEX weekly = 2025-09-04 (Sept 2 2025 was Tuesday under old regime). 5paisa + Business Today + Kotak Securities cite |

### Resulting **6 backfill streams** (with date-range gating)

| # | Stream | Underlying | Cycle | Active window | Total trading days | Liquidity note |
|---|---|---|---|---|---|---|
| 1 | NIFTY-WEEK    | NIFTY     | WEEK  | 2021-01-01 → today | ~1,250 | normal |
| 2 | NIFTY-MONTH   | NIFTY     | MONTH | 2021-01-01 → today | ~1,250 | normal |
| 3 | BANKNIFTY-WEEK| BANKNIFTY | WEEK  | 2021-01-01 → **2024-11-13** | ~960 | normal; dormant after |
| 4 | BANKNIFTY-MONTH| BANKNIFTY| MONTH | 2021-01-01 → today | ~1,250 | normal; day-of-week varies (see §2 BANKNIFTY table) |
| 5 | SENSEX-WEEK   | SENSEX    | WEEK  | **2021-01-01** → today | ~1,250 | **thin pre-2023-05-15** (BSE FNO illiquid; data exists per BSE Notice 20230327-65 — may contain long NaN runs at far OTM strikes) |
| 6 | SENSEX-MONTH  | SENSEX    | MONTH | **2021-01-01** → today | ~1,250 | thin pre-2023-05-15 (same caveat) |

Each stream backfills 21 strikes (ATM ± 10) × 2 types (CALL + PUT) = 42
series. **Total active series = 6 × 42 = 252.**

Day-of-week regime changes within a stream are NOT separately
backfilled — Dhan's rolling endpoint automatically follows whatever
day-of-week was the active expiry on each historical date. The
per-bar `strike` value in the response (see §3) tells us which absolute
contract each bar refers to, so cross-regime stitching is transparent.

> **Decision rationale (operator pushback + 3-agent cross-verification 2026-05-15):**
>
> Phase 0 v1 draft (4 streams, no BANKNIFTY-WEEK, no SENSEX-MONTH, SENSEX
> from 2023-05-15) was hit by two corrections:
> 1. Operator flagged BANKNIFTY weekly DID exist 2021 → Nov 2024 —
>    dropping it lost ~3.9 years of the highest-volume BN options data.
> 2. Cross-verification agent (BSE-domain) flagged SENSEX options WERE
>    tradable 2021-01-01 → 2023-05-12 on Thursday cycle (BSE Notice
>    20230327-65). Pre-relaunch volume was near-zero but contracts
>    were listed and ingestible.
>
> Resolution: 6 streams with full date gating. Backfill cost: ~15.7K
> Dhan calls / ~87 min wall time / ~5 GB storage. Strategy flexibility
> gained: full W vs M A/B test capability + BN historical (2021-2024)
> + complete SENSEX historical (2021-2026). The pre-2023-05-15 SENSEX
> data is annotated thin — sweep can be configured to skip it, but the
> bytes are ingested anyway (cheap insurance, no re-backfill needed).
>
> An even simpler "current-expiry-only" model (3 streams, one per
> underlying) was considered and rejected: the API requires explicit
> `expiryFlag` (WEEK|MONTH) — there is no auto-current mode. See §3.

---

## 3. Endpoint (locked, corrected 2026-05-15 against Dhan SDK source)

### `POST /v2/charts/rollingoption` (Dhan v2; SDK function `expired_options_data`)

Field names verified from the Dhan official Python SDK source
(`src/dhanhq/_historical_data.py:expired_options_data`) and Dhan v2
docs at `https://dhanhq.co/docs/v2/expired-options-data/` on 2026-05-15.

#### Request body

```json
{
  "securityId": "13",              // underlying security ID — 13 / 25 / 51
  "exchangeSegment": "NSE_FNO",    // or "BSE_FNO" for SENSEX
  "instrument": "OPTIDX",          // index options
  "expiryFlag": "WEEK",            // "WEEK" or "MONTH" — SDK validator enforces
  "expiryCode": 0,                 // int 0/1/2/3 — current + 3 forward cycles
  "strike": "ATM",                 // string "ATM" / "ATM+1" .. "ATM+10" / "ATM-1" .. "ATM-10"
  "drvOptionType": "CALL",         // "CALL" or "PUT" — NOT "CE"/"PE"
  "requiredData": ["open","high","low","close","volume","iv","oi","spot","strike"],
  "fromDate": "2024-05-01",        // YYYY-MM-DD IST
  "toDate":   "2024-05-30",        // YYYY-MM-DD IST (≤ 30 days span)
  "interval": 1                    // int 1 / 5 / 15 / 25 / 60 — Track 2 uses 1 ONLY
}
```

**Strike-radius rule (Dhan-enforced):**
- Index near-expiry: ATM ± 10
- Index non-near-expiry (`expiryCode ≥ 1`): ATM ± 3 only
- Stock options: ATM ± 3 (out of Track 2 scope)

#### Response body

```json
{
  "data": {
    "ce": {
      "open":      [...float...],
      "high":      [...float...],
      "low":       [...float...],
      "close":     [...float...],
      "volume":    [...int...],
      "iv":        [...float...],   // implied volatility, fraction (0.2 = 20%)
      "oi":        [...int...],     // open interest, lots
      "spot":      [...float...],   // underlying spot at the same bar
      "strike":    [...float...],   // absolute strike of the contract at this bar (rolls with ATM)
      "timestamp": [...epoch_seconds...]   // UNIX epoch seconds (timezone-neutral)
    },
    "pe": null    // null when drvOptionType=CALL; populated when drvOptionType=PUT
  }
}
```

Arrays parallel-indexed; index `i` = same 1m bar across all 10 arrays.

**Per-bar `strike` array confirms ATM-rolling semantics:** the contract
that backs `strike="ATM+5"` at bar `i=100` may not be the same
contract that backs it at bar `i=1000` — the rolling endpoint stitches
across contracts as the underlying spot moves and ATM shifts. The
per-bar `strike` value tells us which absolute strike the bar refers to.

#### Coverage window

- Up to **30 days per call** (Dhan-enforced).
- **5y backfill** (Track 2 default range): page over 30-day chunks.
- Per-underlying validity windows (see §2): SENSEX options only exist
  2021-01-01 onwards on Thursday cycle (thin pre-2023-05-15 per BSE Notice 20230327-65); BANKNIFTY-WEEK only exists 2021-01-01 → 2024-11-13.

#### Rate limit

**5 req/s + 100,000/day** (Dhan freshdesk article; same as Track 1
Data APIs). A newer Dhan support article says "10 r/s for Data APIs"
but the historical-data subset remained 5 r/s; we use the conservative
limit. Adapter caps at **3 r/s effective** (5 × 0.6 safety factor,
shared token bucket with Track 1).

---

## 4. Timeframes (locked: 1m only)

Track 2 stores **1-minute bars only**. No resampling.

Rationale:
- Options premium is highly nonlinear in spot; resampling 1m → 5m / 15m
  loses path information (e.g., wick that touched ATM-5 IV spike).
- Greeks are bar-local; resampling muddies them.
- 5y × 6 streams × 21 strikes × 2 types × 375 minutes/day at ~50 bytes/bar
  ≈ 5.1 GB total — small enough to keep at 1m.

When the engine needs a higher timeframe (e.g., to align with a Track 1
15m signal), it queries 1m bars and the engine timestamps the entry/exit
to the appropriate 1m bar; the engine never resamples options data.

---

## 5. Strike model (locked)

### ATM definition

The ATM strike for an underlying at any 1-minute bar is the strike
**closest to the underlying's spot price at that bar**, on the per-underlying
**strike grid**:

| Underlying | Strike step | Example grid (around 25,000) |
|---|---|---|
| NIFTY 50 | **50 pts** | 24,950 / 25,000 / 25,050 / … |
| BANKNIFTY | **100 pts** | 49,900 / 50,000 / 50,100 / … |
| SENSEX | **100 pts** | 80,900 / 81,000 / 81,100 / … |

(Strike steps are Dhan-published; smoke test verifies them quarterly.)

### ATM-relative offsets (sent as a STRING — verified)

`strike="ATM+5"` means "5 strike steps above ATM at the bar in
question." For NIFTY at spot 25,012:

- ATM strike = 25,000 (round-to-nearest)
- `strike="ATM"` → strike 25,000
- `strike="ATM+5"` → strike 25,250 (= 25,000 + 5 × 50)
- `strike="ATM-3"` → strike 24,850 (= 25,000 − 3 × 50)

Valid string values: `ATM`, `ATM+1`..`ATM+10`, `ATM-1`..`ATM-10`
(21 strikes for index near-expiry).

**The endpoint stitches across actual contracts behind the scenes:**
when ATM rolls from 25,000 to 25,050, the `strike="ATM"` series
automatically jumps to whatever contract is the new ATM. The response's
**per-bar `strike` array** records the absolute strike at each bar —
this is how we detect rollover boundaries deterministically. No NaN
needed: a rollover is detected when `strike[i] ≠ strike[i-1]` for
adjacent bars with `bar_ts[i] = bar_ts[i-1] + 60_000_000` (one minute).

---

## 6. Trade horizon (locked)

Track 2 trades the **current expiry** for the underlying:

| Underlying | Default trade expiry | When monthly is used |
|---|---|---|
| NIFTY | WEEK (current Tuesday weekly) | If signal closes within 0-2 days of Tuesday, also test MONTH |
| BANKNIFTY | MONTH (last Tuesday) | Always (no weekly available) |
| SENSEX | WEEK (current Thursday weekly) | If signal closes within 0-2 days of Thursday, also test MONTH |

"Current expiry at sweep time" means: at the simulated bar's wall
time, pick the next not-yet-expired expiry of the chosen cycle. The
engine NEVER looks at expiries that have not been published yet (e.g.,
NIFTY weekly for the week after next is not visible until the prior
week's expiry passes).

---

## 7. Greeks (locked)

| Greek | Source | Why |
|---|---|---|
| **IV** (Implied Volatility) | **Dhan** — returned as `implied_volatility` array | Dhan computes it from their option pricer; we don't second-guess |
| **Delta** | **Computed** (Black-Scholes-Merton) | Standard; needed for hedge-ratio + ATM definition |
| **Gamma** | **Computed** (BSM) | Needed for risk metrics + early-exit Greek thresholds |
| **Theta** | **Computed** (BSM) | Daily decay; critical for short premium |
| **Vega** | **Computed** (BSM) | IV sensitivity for stress tests |
| Vanna, charm, color | NOT computed | Out of scope for Phase 4 first sweep |

### Black-Scholes-Merton inputs

| Input | Source |
|---|---|
| S (spot) | Track 1's spot bar for the underlying at the same timestamp |
| K (strike) | Derived from ATM-relative offset + per-underlying strike step |
| T (time to expiry, in years) | (expiry timestamp at 15:30 IST − bar timestamp) / 365.25 / 86400 |
| r (risk-free rate) | **3-month T-bill yield**, monthly snapshot, hardcoded as a 60-row table in `brutex/options/risk_free.py` (Phase 2 deliverable) |
| σ (volatility) | Dhan's `implied_volatility` for the same bar |
| q (dividend yield) | **0** for all 3 indices (index points are price-weighted; no underlying dividend stream)  |

### BSM implementation

- Pure Rust `fno_math::bsm::{call_price, put_price, delta, gamma, theta, vega}`.
- Float64 inputs, Float64 outputs.
- Cross-validated against `py_vollib` (CPython, 1e-10 tolerance) in the
  Phase 1 BSM oracle test (`tests/integration/test_bsm_vs_pyvollib.py`).
- Greeks at every bar are precomputed and stored in the Parquet
  alongside OHLC + IV (not lazy — needed at engine evaluation time).

---

## 8. Brokerage / STT / STO / SEBI / GST cost model (TBD — Phase 3)

This is the single largest unknown in Track 2 and the reason for the
"first sweep" being scheduled in Phase 4. The cost model lives at:

- `brutex/options/costs.py` (Phase 3 deliverable)
- Verified table at `docs/track2/COSTS_VERIFIED.md` (Phase 3 deliverable)

Categories to verify before Phase 4:
1. Dhan brokerage per options leg (₹ flat or % capped) — cite Dhan tariff page
2. STT — 0.05% on sell side options (on premium); cite NSE/BSE circular
3. STO (Securities Transaction on Options) — varies by exercise; cite circular
4. SEBI turnover charge — current rate is 0.0001% (per NSE/BSE)
5. NSE/BSE transaction fee per leg
6. GST — 18% on (brokerage + SEBI + exchange)
7. Stamp duty — 0.003% buy side

All numbers must be CITED with their source URL and verified date.
No hallucinated tariffs.

---

## 9. Slippage model (TBD — Phase 3)

Three slippage knobs to test in Phase 4 sweep:

| Knob | Range | Default |
|---|---|---|
| `slippage_premium_pct` | 0 / 0.1 / 0.25 / 0.5 / 1.0% | 0.25% |
| `slippage_bid_ask_half` | 0 / 25 / 50 / 100% of spread | 50% |
| `slippage_partial_fill_pct` | 100 / 90 / 75 / 50% of quantity | 100% |

Sweep cardinality = 5 × 4 × 4 = 80 slippage profiles per (signal, strike, expiry).

---

## 10. Storage + backfill estimate (revised after 3-agent verification)

Per the Phase 0 storage + history-timeline + API agents (all 2026-05-15):

### Series count

| Stream | Underlying | Cycle | Active days in window | Strikes × Types | Series |
|---|---|---|---|---|---|
| 1 | NIFTY     | WEEK  | ~1,250 | 21 × 2 | 42 |
| 2 | NIFTY     | MONTH | ~1,250 | 21 × 2 | 42 |
| 3 | BANKNIFTY | WEEK  | ~960   | 21 × 2 | 42 |
| 4 | BANKNIFTY | MONTH | ~1,250 | 21 × 2 | 42 |
| 5 | SENSEX    | WEEK  | ~1,250 | 21 × 2 | 42 |
| 6 | SENSEX    | MONTH | ~1,250 | 21 × 2 | 42 |
| | | | **Total active series** | | **252** |

### Backfill calls + wall time

| Quantity | Calculation | Value |
|---|---|---|
| Calendar days in window | 2021-01-01 → 2026-05-15 | ~1,960 |
| Chunks per series (30-day chunks) | Per-stream window / 30 | NIFTY: 65 / BN-WEEK: 47 / BN-MONTH: 65 / SENSEX: 65 |
| Calls — NIFTY (2 streams) | 42 × 65 × 2 | 5,460 |
| Calls — BANKNIFTY-WEEK | 42 × 47 | 1,974 |
| Calls — BANKNIFTY-MONTH | 42 × 65 | 2,730 |
| Calls — SENSEX (2 streams) | 42 × 65 × 2 | 5,460 |
| **Total backfill calls** | | **~15,624** |
| Effective rate (3 r/s cap) | adapter shared token bucket | 3 calls/sec |
| Backfill wall time | 15,624 / 3 = 5,208 sec | **~87 minutes** |
| Daily quota used | 15,624 / 100,000 | **15.6%** (one-time, below cap) |

### Storage

| Quantity | Value |
|---|---|
| Bars per trading day | 375 (09:15–15:30 IST, 1-minute) |
| Bytes per bar (Parquet ZSTD, 16 columns) | ~50 |
| Bytes per series (full 5y, ~1,250 days) | 375 × 1,250 × 50 = 23.4 MB |
| Bytes per series (BN-WEEK active ~4y, ~960 days) | 375 × 960 × 50 = 18 MB |
| Bytes per series (SENSEX 2021-2023-05-12 thin Thursday, ~600 days × low-fill) | 375 × 600 × 50 × ~0.3 fill ≈ 3.4 MB |
| Bytes per series (SENSEX 2023-05-15 → today, ~700 normal days) | 375 × 700 × 50 = 13 MB |
| Total: NIFTY 2 streams × 42 series × 23.4 MB | 1.97 GB |
| Total: BANKNIFTY WEEK 42 × 18 MB | 0.76 GB |
| Total: BANKNIFTY MONTH 42 × 23.4 MB | 0.98 GB |
| Total: SENSEX 2 streams × 42 series × (13 + 3.4) MB | 1.38 GB |
| **Total options lake (5y, 252 series)** | **~5.1 GB** |

### Daily incremental (post-backfill)

| Quantity | Value |
|---|---|
| Active series on a given day (excludes BN-WEEK after Nov 2024) | 210 (252 minus 42 dormant BN-WEEK) |
| Calls per day | 210 |
| Daily quota used | 210 / 100,000 = **0.21%** |
| Daily wall time | 210 / 3 = 70 sec |

Total disk: Track 1 spot lake (~80 MB) + Track 2 options lake (~5.1 GB)
≈ 5.2 GB. Compute lake (results.parquet) is the dominant disk user.

> **Note on SENSEX thin pre-2023-05-15 data:** BSE FNO segment was
> illiquid before the May 2023 relaunch (per Business Standard 2023-05-15:
> first-day post-relaunch turnover ~₹53 crore vs NSE ₹lakh-crores/day).
> Many far-OTM strikes will return NaN-heavy series. Storage estimate
> assumes ~30% fill rate for this window — actual could be lower.
> Sweep config can filter strategies that touch the thin window if
> liquidity floor required.

---

## 11. Money representation (locked)

**Same as Track 1: int64 paisa everywhere.**

- Option premium open = `int64` paisa (e.g., 12,575 = ₹125.75)
- P&L = `int64` paisa
- Brokerage = `int64` paisa
- ATM strike = `int64` (NIFTY 25,000; not paisa)

No floats in hot paths. BSM computes Greeks as float64; the engine
converts BSM-derived deltas / Greeks to the int64 P&L only at fill
time (one conversion per fill, not per bar).

---

## 12. Audit trail (inherits Track 1)

Same audit log as Track 1. Track 2 adds these audit kinds:

| audit_kind | Payload (compact) |
|---|---|
| `OPT_INGEST_CHUNK_OK` | (underlying, cycle, strikePosition, optionType, from, to, n_bars, n_NaN, sha256_first_64hex) |
| `OPT_INGEST_CHUNK_ERR` | (underlying, cycle, strikePosition, optionType, from, to, error_code, retry_count) |
| `OPT_GREEK_COMPUTED` | (underlying, cycle, strikePosition, optionType, bar_ts, IV, delta, gamma, theta, vega) — sampled (1 per 1000 bars) |
| `OPT_BACKTEST_ENTRY` | (signal_hash, underlying, strike, optionType, entry_premium_paisa, slippage_profile_hash) |
| `OPT_BACKTEST_EXIT` | (signal_hash, exit_reason, exit_premium_paisa, pnl_paisa) |

Audit chain SHA256 continues from Track 1 — never resets.

---

## 13. Connection to Track 1

Track 1 ships first. Track 2 connects via:

1. **Spot data dependency:** Track 2 engine reads Track 1's spot lake
   (1m bars for NIFTY/BANKNIFTY/SENSEX) at the same timestamps as
   options bars. Look-ahead enforcement: at bar N, options engine sees
   spot bar N too — never N+1.

2. **Signal input:** Track 1 produces a top-3 list of `(strategy_mask,
   direction, instrument, tf)`. Track 2 takes each tuple and:
   - Replays Track 1 signal to find entry/exit timestamps
   - For each entry, picks `(option_strike, option_type, expiry_cycle)`
     and simulates the options trade
   - Returns option-realistic P&L vector

3. **VIX stamps:** Inherits Track 1's `vix_at_entry` / `vix_at_exit`
   columns on the result row.

4. **Audit chain:** Continues from Track 1's audit chain.

5. **Doctor:** Adds C12 (options-fresh) check to `brutex doctor`.

---

## 14. Effort estimate

| Phase | Estimate |
|---|---|
| Phase 0 — Documentation (this PR) | 2-3 days |
| Phase 1 — Verification scaffolding | 1 week |
| Phase 2 — Dhan options adapter (BLOCKED on Track 1 Phase 2 ship) | 2 weeks |
| Phase 3 — Backtest engine + cost model | 2-3 weeks |
| Phase 4 — First sweep + ship | 1 week |
| **Total Track 2 build** | **6-8 weeks** AFTER Track 1 Phase 2 ships |

Track 2 work can start when Track 1 Phase 2 is ✅ DONE. Phase 0 and
Phase 1 (against fakes) can proceed in parallel with Track 1 Phase 1.

---

## 15. Verification gates (extends Track 1's 9-layer pyramid)

| Layer | Track 1 invariants (current count) | Track 2 adds | New total |
|---|---|---|---|
| L1 unit | many | ~30 | many |
| L2 property | 26 | **+11 options-specific** | 37 |
| L3 integration / oracle | 4 | **+2** (options replay + BSM-vs-py_vollib) | 6 |
| L4 golden / fixtures | yes | options replay golden | yes |
| L5 contract | yes | rollingoption contract | yes |
| L6 audit-chain | yes | options audit kinds | yes |
| L7 bench (O(1)) | yes | options bar lookup; Greeks compute | yes |
| L8 stress | yes | 24h options replay | yes |
| L9 dashboard / smoke | yes | options health gate (C12) | yes |

**11 Track 2-specific property invariants:**

| ID | Invariant |
|---|---|
| P-O1 | ATM-rolled rollingoption series has monotonic timestamps within each contract span |
| P-O2 | Premium ≥ 0 always; volume ≥ 0; OI ≥ 0 |
| P-O3 | OHLC consistency: `low ≤ open, close ≤ high` |
| P-O4 | Strike step = published step (50 for NIFTY, 100 for BANKNIFTY/SENSEX) |
| P-O5 | strikePosition ∈ [-10, +10] |
| P-O6 | At expiry-day 15:30 IST, time-to-expiry T ≤ 1/(365.25 × 86400) |
| P-O7 | Delta(CE) ∈ [0, 1]; Delta(PE) ∈ [-1, 0] |
| P-O8 | Gamma ≥ 0 always (call and put) |
| P-O9 | Theta ≤ 0 always (long premium decays) |
| P-O10 | put-call parity: \|C − P − (S − Ke^{-rT})\| ≤ tol (10 paisa) for each bar |
| P-O11 | Audit chain unbroken across mixed Track1+Track2 ingest |

**2 Track 2-specific oracle tests:**

| ID | Oracle |
|---|---|
| O-O1 | Options-replay byte-identical: replay the audit log; result Parquet bytes must match the original sweep run |
| O-O2 | Black-Scholes-Merton parity: BSM Greeks vs `py_vollib.black_scholes_merton` to 1e-10 absolute, across a 10K-sample synthetic grid |

---

## 16. Risk register (Phase 0 scope)

| Risk | Mitigation |
|---|---|
| Dhan changes `/charts/rollingoption` schema | Contract test in Phase 1 detects schema drift before live ingest |
| Dhan removes ATM-relative endpoint and we have to fall back to per-contract | Phase 2 abstraction layer (`OptionsBackfillStrategy` interface) lets us swap with a 1-week refactor |
| SEBI changes expiry cycles again | All expiry-day logic centralized in `brutex/options/expiry_calendar.py` — single-point fix |
| BSM model insufficient for far-OTM (smile not captured) | First sweep uses ATM ± 5 by default; ATM ± 10 only for stress test of how dependent the strategy is on far-OTM Greeks |
| Risk-free rate table goes stale | Annual refresh + smoke test "rate < 6 months old" |
| Brokerage formula changes mid-year | Cost model versioned; old runs replay with their original cost version |

---

## 17. Decisions to lock (#200-#220 estimated)

These will be appended to `docs/MIGRATION.md` when Phase 0 PR merges:

- #200: Track 2 = options realism leg of BruteX (post-Track-1)
- #201: Endpoint `/v2/charts/rollingoption` (ATM-relative, not per-contract)
- #202: 3 underlyings (NIFTY/BANKNIFTY/SENSEX) — NO India VIX options
- #203: ATM ± 10 strike radius, hardcoded; rationale = Dhan-enforced
- #204: 1-minute bars ONLY; no resampling
- #205: Expiry cycle history reconstructed for 5y window from primary sources (BSE Notice 20230327-65; NSE FAOP/57540, 60011, 64506, 65336, 68747; SEBI master circular 2024-10-01); withdrawn Monday-window 2025-Apr explicitly excluded
- #206: **6 backfill streams** (NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-WEEK [until 2024-11-13], BANKNIFTY-MONTH, SENSEX-WEEK, SENSEX-MONTH; SENSEX windows extend back to 2021-01-01 per BSE Notice 20230327-65 with thin-liquidity annotation)
- #207: IV from Dhan; Greeks computed (Black-Scholes-Merton)
- #208: Risk-free rate from 3-month T-bill monthly table (60 rows, 5y)
- #209: Dividend yield q = 0 for all 3 indices
- #210: Storage estimate ~5.1 GB for 5y (revised after S1 verification)
- #211: Backfill ~15,624 calls / ~87 min wall time / 15.6% daily quota (revised)
- #212: Cost model lives at `brutex/options/costs.py`, version-stamped
- #213: Slippage profile = 5 × 4 × 4 = 80 combos per (signal, strike, expiry)
- #214: Money is int64 paisa (same as Track 1)
- #215: Audit kinds OPT_INGEST_*/OPT_GREEK_*/OPT_BACKTEST_* added
- #216: 11 Track 2 property invariants added (Phase 1)
- #217: 2 Track 2 oracle tests added (Phase 1)
- #218: `brutex doctor` C12 (options-fresh) check added
- #219: Track 2 effort = 6-8 weeks AFTER Track 1 Phase 2 ships
- #220: Track 2 Phase 0 4-agent hostile review pass

(Final numbering may shift if Track 1 adds more decisions before Track 2 merges.)

---

## 18. References (verified 2026-05-15 by 3-agent cross-verification)

### Track 1 + API
| Source | Detail |
|---|---|
| `docs/TRACK1_BACKTEST_SPEC.md` | Parent spec |
| Dhan v2 API docs | dhanhq.co/docs/v2/expired-options-data/ |
| Dhan Python SDK | `dhanhq.sdk.dhanhq.expired_options_data` (source verified 2026-05-15) |
| Dhan annexure | dhanhq.co/docs/v2/annexure (segment + instrument enums) |
| Dhan instrument master | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` |
| `py_vollib` | https://github.com/vollib/py_vollib (BSM oracle) |

### Primary regulatory + exchange sources (per-claim citations)

| Subject | Source | URL / ID |
|---|---|---|
| NIFTY 50 weekly launch (2019-02-11) | NSE circular 39675 (2018-12-18) | `nsearchives.nseindia.com/content/circulars/39675` |
| NIFTY 50 + BANKNIFTY Sep-2025 Tuesday shift | NSE FAOP/68747 | `nsearchives.nseindia.com/content/circulars/FAOP68747.pdf` |
| Nov 2024 weekly discontinuation (NSE side) | NSE FAOP/64506 (2024-10-10) | `nsearchives.nseindia.com/content/circulars/FAOP64506.pdf` |
| BANKNIFTY weekly Wed shift (Sep 2023) | NSE 119/2023 = FAOP57540 (2023-07-12) | `archives.nseindia.com/content/circulars/FAOP57540.pdf` |
| BANKNIFTY monthly Wed shift (Mar 2024) | NSE FAOP/60011 (2023-12-29) | `nsearchives.nseindia.com/content/circulars/FAOP60011.pdf` |
| BANKNIFTY monthly Wed→Thu revert (Jan 2025) | NSE FAOP/65336 (eff. EOD 2025-01-01) | `nsearchives.nseindia.com/content/circulars/FAOP65336.pdf` |
| BANKNIFTY weekly launch (2016-05-27) | Business Standard / PTI 2016-05-05 | `business-standard.com/article/pti-stories/nse-to-launch-weekly-options-contracts-on-bank-nifty-index-116050501487_1.html` |
| Last BANKNIFTY weekly expiry 2024-11-13 | Zeebiz feature 2024-11-13 | `zeebiz.com/market-news/news-bank-nifty-weekly-expiry-ends-today-...325884` |
| Withdrawn Monday-expiry window (Mar 2025) | Zerodha bulletin 407122; Business Standard 2025-03-27 | `zerodha.com/marketintel/bulletin/407122/...`; `business-standard.com/markets/.../125030500178_1.html` |
| SENSEX pre-relaunch Thursday cycle 2021-2023 | **BSE Notice 20230327-65** (primary) | `bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20230327-65` |
| SENSEX/BANKEX May-2023 relaunch (Friday) | Business Standard 2023-05-15 | `business-standard.com/markets/news/bse-relaunches-sensex-bankex-derivatives-contracts-to-boost-trading-123051501206_1.html` |
| BANKEX shift Fri→Mon (Oct 2023) | Business Standard 2023-08-30 | `business-standard.com/markets/news/bse-moves-bankex-expiry-to-monday-from-friday-...-123083000569_1.html` |
| SENSEX/BANKEX Jan-2025 Tuesday shift | Business Standard 2024-10-04; 5paisa Jan 2025 | `business-standard.com/markets/capital-market-news/bse-discontinues-weekly-contracts-on-bankex-sensex-50-124100400441_1.html` |
| SENSEX Sept-2025 Thursday shift (first Thu = 2025-09-04) | 5paisa SEBI reshuffle; Kotak Securities | `5paisa.com/news/sebi-reshuffles-equity-derivatives-expiry-days-to-streamline-trading` |
| BSE weekly discontinuation Nov 2024 (SENSEX 50, BANKEX) | Business Standard 2024-10-04 + Business Today 2024-10-03 | (URLs as above) |
| SEBI master circular (underlying source) | SEBI Oct-1-2024 "Measures to strengthen index derivatives framework for increased investor protection" | `sebi.gov.in` |
| FINNIFTY launch (2021-01-11) | NSE press release 2020-12-10 | `nseindia.com/nse-to-launch-derivatives-on-the-nifty-financial-services-index-from-january-11-2021` |
| FINNIFTY Tue shift (Oct 2021) | NSE FAOP49595 | `www1.nseindia.com/content/circulars/FAOP49595.pdf` |
| MIDCPNIFTY F&O launch (2022-01-24) | NSE FAOP54335 | `archives.nseindia.com/content/circulars/FAOP54335.pdf` |
| MIDCPNIFTY Wed→Mon shift (Aug 2023) | NSE FAOP56128 | `archives.nseindia.com/content/circulars/FAOP56128.pdf` |
| NIFTY NEXT 50 launch (2024-04-24, monthly-only) | Business Standard 2024-04-18 | `business-standard.com/markets/stock-market-news/nse-to-launch-derivatives-on-nifty-next-50-index-from-apr-24-gets-sebi-nod-124041800743_1.html` |

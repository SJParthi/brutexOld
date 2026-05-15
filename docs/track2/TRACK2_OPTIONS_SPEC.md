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

### Expiry cycles (locked, per SEBI mandates as of 2026-05-15)

| Underlying | Weekly expiry | Monthly expiry | Notes |
|---|---|---|---|
| NIFTY 50 | **Tuesday** | Last Tuesday of month | SEBI mandate Sep 2025; pre-Sep-2025 weekly was Thursday |
| BANKNIFTY | ❌ **NONE** (killed Nov 2024) | Last Tuesday of month | SEBI mandate Nov 2024; weekly contracts stopped |
| SENSEX | **Thursday** | Last Thursday of month | SEBI mandate Sep 2025; pre-Sep-2025 weekly was Tuesday |

Resulting **4 backfill streams** (one row = one rollingoption series
to backfill):

| Stream | Underlying | Expiry cycle | Strike radius | Types | Series count per day |
|---|---|---|---|---|---|
| 1 | NIFTY | WEEK | ATM ± 10 | CE+PE | 42 |
| 2 | NIFTY | MONTH | ATM ± 10 | CE+PE | 42 |
| 3 | BANKNIFTY | MONTH | ATM ± 10 | CE+PE | 42 |
| 4 | SENSEX | WEEK | ATM ± 10 | CE+PE | 42 |

A SENSEX MONTH stream is NOT separately backfilled — the SENSEX
monthly contract IS the last Thursday SENSEX WEEK contract by
convention. Phase 2 verifies this against Dhan's expiry calendar.

A BANKNIFTY WEEK stream does NOT exist (SEBI killed it Nov 2024).

---

## 3. Endpoint (locked)

### `POST /v2/charts/rollingoption` (Dhan v2)

Verified from the Dhan official Python SDK source
(`dhanhq.sdk.dhanhq:get_charts_rollingoption`) on 2026-05-15.

#### Request body

```json
{
  "underlying": "NIFTY",          // or "BANKNIFTY" or "SENSEX"
  "exchangeSegment": "NSE_FNO",   // or "BSE_FNO" for SENSEX
  "instrument": "OPTIDX",
  "expiryCycle": "WEEK",          // or "MONTH"
  "optionType": "CE",             // or "PE"
  "strikePosition": -10,          // int, -10..+10 (ATM-relative)
  "fromDate": "2024-05-01",       // YYYY-MM-DD IST
  "toDate":   "2024-05-30",       // YYYY-MM-DD IST (≤ 30 days span)
  "interval": "1"                 // 1-minute ONLY for Track 2
}
```

#### Response body (1-minute bars, IST trading hours)

```json
{
  "open":      [...float...],     // option premium open
  "high":      [...float...],
  "low":       [...float...],
  "close":     [...float...],
  "volume":    [...int...],
  "open_interest": [...int...],
  "implied_volatility": [...float...],
  "timestamp": [...epoch_seconds_IST...]
}
```

Arrays are parallel; index `i` corresponds to the same 1m bar.

#### Coverage window

- Up to **30 days per call** (Dhan-enforced).
- **5y backfill** (Track 2 default range): we'll page over 30-day chunks.

#### Rate limit

Same Dhan data-API limit as Track 1: **5 req/s + 100K/day**. Adapter
caps at 3 r/s effective (5 × 0.6 safety factor, same as Track 1).

---

## 4. Timeframes (locked: 1m only)

Track 2 stores **1-minute bars only**. No resampling.

Rationale:
- Options premium is highly nonlinear in spot; resampling 1m → 5m / 15m
  loses path information (e.g., wick that touched ATM-5 IV spike).
- Greeks are bar-local; resampling muddies them.
- 5y × 4 streams × 21 strikes × 2 types × 375 minutes/day at ~50 bytes/bar
  ≈ 3-4 GB total — small enough to keep at 1m.

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

### ATM-relative offsets

`strikePosition = +5` means "5 strike steps above ATM at the bar in
question." For NIFTY at spot 25,012:

- ATM strike = 25,000 (round-to-nearest)
- `strikePosition=0` → strike 25,000
- `strikePosition=+5` → strike 25,250 (= 25,000 + 5 × 50)
- `strikePosition=-3` → strike 24,850 (= 25,000 − 3 × 50)

**The /charts/rollingoption endpoint stitches across actual contracts**
behind the scenes — when ATM rolls from 25,000 to 25,050, the
`strikePosition=0` series automatically jumps to whatever contract is
the new ATM. The discontinuity is data-encoded as a NaN row or a
fresh-OHLC bar; Phase 2 quantifies which.

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

## 10. Storage estimate

Per the Phase 0 storage agent (2026-05-15):

| Dimension | Count |
|---|---|
| Streams | 4 (NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-MONTH, SENSEX-WEEK) |
| Strikes per stream | 21 (ATM ± 10) |
| Types per strike | 2 (CE, PE) |
| Series per stream | 21 × 2 = 42 |
| Total series | 4 × 42 = **168** |
| Bars per trading day | 375 (09:15–15:30 IST, 1-minute) |
| Trading days in 5y | ~1,250 (5y × 250) |
| Bars per series per 5y | 375 × 1,250 = **468,750** |
| Bytes per bar (with Greeks) | ~50 (compressed Parquet, ZSTD) |
| Bytes per series per 5y | 468,750 × 50 = 23.4 MB |
| Total storage (5y, 168 series) | 168 × 23.4 MB = **3.93 GB** |

This is a small fraction of disk; 2 TB on the M4 Pro external SSD is
plenty. Track 1 spot lake (~80 MB) + Track 2 options lake (~4 GB) ≈
4.1 GB total. Compute lake (results.parquet) is the dominant disk user.

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
- #205: Expiry cycles per SEBI mandate Sep 2025 + Nov 2024 (NIFTY=Tue, BANKNIFTY=MONTH only, SENSEX=Thu)
- #206: 4 backfill streams (NIFTY-WEEK, NIFTY-MONTH, BANKNIFTY-MONTH, SENSEX-WEEK)
- #207: IV from Dhan; Greeks computed (Black-Scholes-Merton)
- #208: Risk-free rate from 3-month T-bill monthly table (60 rows, 5y)
- #209: Dividend yield q = 0 for all 3 indices
- #210: Storage estimate ~3-4 GB for 5y
- #211: Backfill ~10,250 calls, ~1 hour wall time, <0.1% daily quota
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

## 18. References

| Source | Detail |
|---|---|
| `docs/TRACK1_BACKTEST_SPEC.md` | Parent spec |
| Dhan v2 API docs | dhanhq.co/docs/v2 — search "rollingoption" |
| Dhan Python SDK | `dhanhq.sdk.dhanhq.get_charts_rollingoption` (source verified 2026-05-15) |
| Dhan annexure | dhanhq.co/docs/v2/annexure (segment + instrument enums) |
| Dhan instrument master | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` |
| SEBI expiry mandate Sep 2025 | NSE/BSE circulars (NIFTY moves to Tuesday; SENSEX moves to Thursday) |
| SEBI weekly contracts mandate Nov 2024 | BANKNIFTY weekly killed; monthly only |
| `py_vollib` | https://github.com/vollib/py_vollib (BSM oracle) |

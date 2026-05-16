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
| 1 | NIFTY-WEEK    | NIFTY     | WEEK  | 2021-01-01 → today | ~1,342 | normal |
| 2 | NIFTY-MONTH   | NIFTY     | MONTH | 2021-01-01 → today | ~1,342 | normal |
| 3 | BANKNIFTY-WEEK| BANKNIFTY | WEEK  | 2021-01-01 → **2024-11-13** | ~966 | normal; dormant after |
| 4 | BANKNIFTY-MONTH| BANKNIFTY| MONTH | 2021-01-01 → today | ~1,342 | normal; day-of-week varies (see §2 BANKNIFTY table) |
| 5 | SENSEX-WEEK   | SENSEX    | WEEK  | **2021-01-01** → today | ~1,342 (~642 thin + ~700 normal) | **thin pre-2023-05-15** (BSE FNO illiquid; data exists per BSE Notice 20230327-65 — may contain long NaN runs at far OTM strikes) |
| 6 | SENSEX-MONTH  | SENSEX    | MONTH | **2021-01-01** → today | ~1,342 (~642 thin + ~700 normal) | thin pre-2023-05-15 (same caveat) |

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
> Resolution: 6 streams with full date gating. Backfill cost: ~15,876
> Dhan calls / ~88 min wall time / ~5.5 GB storage. Strategy flexibility
> gained: full W vs M A/B test capability + BN historical (2021-2024)
> + complete SENSEX historical (2021-2026). The pre-2023-05-15 SENSEX
> data is annotated thin — sweep can be configured to skip it, but the
> bytes are ingested anyway (cheap insurance, no re-backfill needed).
>
> An even simpler "current-expiry-only" model (3 streams, one per
> underlying) was considered and rejected: per the Dhan SDK validator
> (`_historical_data.py`) the API requires explicit `expiryFlag`
> ∈ {WEEK, MONTH} — there is no auto-current mode. See §3. **Phase 1
> contract test C-2 will verify this against the live HTTP endpoint,
> in case the validator is SDK-side defensive rather than API-enforced.**

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
- 5.37y × 6 streams × 21 strikes × 2 types × 375 minutes/day at ~50 bytes/bar
  ≈ 5.5 GB total — small enough to keep at 1m.

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

## 6b. Strike Selection Rules (LOCKED 2026-05-16) ⭐

Operator decision 2026-05-16: when a Track 1 signal fires, the engine
must pick a strike to trade. Three options were considered:

| Approach | Verdict |
|---|---|
| Cherry-pick best in hindsight | ❌ REJECTED — classic in-sample overfitting trap |
| Report distribution of all 21 strikes | Considered, but doesn't produce executable strategies |
| **Pre-trade rule** — sweep over rules per signal | ✅ **LOCKED** |

### The 10 pre-trade strike-selection rules

For each Track 1 signal, the engine evaluates the strategy under
EACH of these 10 rules and records the PnL per rule. The rule is
part of the strategy mask — sweeping rules is part of the brute-force
grid. No hindsight bias because the rule is fixed BEFORE the trade.

| # | Rule name | What it does | Notes |
|---|---|---|---|
| 1 | `atm` | Pick the strike closest to spot price at signal time | Highest liquidity, tightest spread |
| 2 | `atm_plus_1_in_dir` | ATM + 1 step in trade direction (CE → strike up; PE → strike down) | Slightly OTM, cheaper premium |
| 3 | `atm_plus_2_in_dir` | ATM + 2 steps in trade direction | More OTM, higher leverage |
| 4 | `atm_plus_5_in_dir` | ATM + 5 steps in trade direction | Aggressive OTM, high gamma |
| 5 | `atm_minus_1_in_dir` | ATM - 1 step in trade direction | Slightly ITM, more premium |
| 6 | `atm_minus_2_in_dir` | ATM - 2 steps in trade direction | More ITM, lower gamma |
| 7 | `atm_minus_5_in_dir` | ATM - 5 steps in trade direction | Deep ITM, premium-heavy |
| 8 | `delta_target_030` | Strike with delta closest to 0.30 (CE) or -0.30 (PE) at signal time | Common OTM trade-off |
| 9 | `delta_target_050` | Strike with delta closest to 0.50 (CE) or -0.50 (PE) — effectively ATM | Greek-based ATM lookup |
| 10 | `delta_target_015` | Strike with delta closest to 0.15 (CE) or -0.15 (PE) | Far OTM, only if liquidity OK |

### Rule semantics — "in trade direction"

| Trade direction | Rule applied to CE | Rule applied to PE |
|---|---|---|
| BULL (buy CE) | `atm_plus_N` → strike higher than ATM (more OTM call) | n/a (bull trades CE) |
| BULL (buy CE) | `atm_minus_N` → strike lower than ATM (more ITM call) | n/a |
| BEAR (buy PE) | n/a (bear trades PE) | `atm_plus_N` → strike LOWER than ATM (more OTM put) |
| BEAR (buy PE) | n/a | `atm_minus_N` → strike HIGHER than ATM (more ITM put) |

So `atm_plus_1_in_dir` means "1 strike further OTM than ATM for the
chosen direction":
- BULL CE: strike = ATM + 1 step (price up)
- BEAR PE: strike = ATM - 1 step (price down)

### Delta-target rules (rules 8, 9, 10)

Delta is computed from the option's bar-time Greeks (BSM, see §7).
The engine scans the 21-strike chain at signal time and picks the
strike whose absolute delta is closest to the target. If two strikes
are equally close, the one closer to ATM wins (tie-breaker).

Implementation note: delta-target rules require the Greeks pipeline
to be operational, so they're enabled in Phase 4 (after Phase 3's
Rust BSM kernel ships). Rules 1-7 (offset-based) work from Phase 2
onwards using just the strike grid.

### Sweep cardinality impact

Per Track 1 signal, the engine produces **10 options PnL records**
(one per rule). With ~5,000 signals across top-10 Track 1 strategies:

| Item | Count |
|---|---|
| Top-10 Track 1 strategies | 10 |
| Signals per strategy | ~500 |
| Pre-trade rules per signal | 10 |
| Total options-trade simulations | **~50,000 per first sweep** |
| Approx compute time | **5-15 min** on Polars lake |

### Why this beats "sweep all 21 strikes and pick best"

| Property | Sweep 21 + pick best | Pre-trade rule sweep |
|---|---|---|
| Hindsight bias | ❌ Yes — picks best in hindsight | ✅ No — rule fixed before trade |
| Trades simulated per signal | 21 × 2 (CE+PE) = 42 | 10 |
| Inflated Sharpe risk | High (2-3× inflated typical) | None |
| Statistical validity | Low | High |
| Produces executable strategy | Ambiguous | ✅ Yes (rule is the strategy) |
| Sweep size | ~210,000 trades | ~50,000 trades |

### Rule lookup table (config-driven)

Lives at `brutex/options/strike_rules.py` (Phase 2 deliverable):

```python
# Each rule is a dataclass with:
# - name: str  (e.g., "atm_plus_2_in_dir")
# - kind: enum  (OFFSET | DELTA_TARGET)
# - param: int OR float  (offset count OR delta target)
@dataclass(frozen=True)
class StrikeRule:
    name: str
    kind: StrikeRuleKind
    param: float
    requires_greeks: bool
```

10 instances at module level, exported as `ALL_STRIKE_RULES`.

### Adding new rules

Per the COMMON · DYNAMIC · SCALABLE · AUTOMATED principle (§Operating
Principle in Track 1 spec): add a new rule by appending to
`ALL_STRIKE_RULES`. No re-architecting. The brute-force engine
automatically sweeps the new rule on the next run. Strategy masks
that referenced specific rule indices remain valid (rules are
identified by name, not index).

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

## 8. Brokerage / STT / Exchange / SEBI / Stamp / GST cost model (LOCKED 2026-05-16)

Research agent verified all rates against ClearTax, Zerodha Z-Connect,
Bajaj Broking, Angel One, NSE, sahi.com, Ventura, PL Capital,
Chittorgarh, IPOGMPINDIA (2026-05-16). **Dhan's official rate card page
returned HTTP 403 on automated fetch** — numbers below are
cross-verified from 3+ independent secondary sources that quote Dhan
directly. Operator must manually verify Dhan's published page before
final code-lock.

### 8.1 Verified constants table

| Charge | Rate | Applied to | Side | Source |
|---|---|---|---|---|
| Dhan brokerage | **₹20.00 flat per order** | per executed order | both legs | dhan.co/pricing (cross-confirmed: ipogmpindia.com 2026, chittorgarh.com, Zerodha review) |
| STT (Securities Transaction Tax) | **0.10% pre-1-Apr-2026; 0.15% post-1-Apr-2026** | premium | sell side only | ClearTax 2026; Budget 2026 (Finnovate); SEBI raised from 0.0625% on Oct 1, 2024 |
| NSE F&O exchange charge | **0.03503%** | premium | both sides | NSE post-Oct-1-2024 SEBI "true-to-label" circular |
| BSE F&O exchange charge | **0.03250%** | premium | both sides | BSE post-Oct-1-2024 (Sensex/Bankex); Angel One verified |
| SEBI turnover fee | **0.0001%** | premium | both sides | NSE: ₹10 per crore |
| IPFT (Investor Protection Fund Trust, NSE) | **0.0005%** | premium | both sides | NSE charge sheet |
| Stamp duty | **0.003%** | premium | buy side only | Indian Stamp Act 2020 (unified rate) |
| GST | **18%** | (brokerage + exchange + SEBI + IPFT) | both sides | CGST 9% + SGST 9% OR IGST 18%; ClearTax 2026 |

**Not in cost basis for GST:** STT, stamp duty (statutory levies, not services).

### 8.2 Round-trip worked example (NIFTY ATM call, ₹100 → ₹120, 1 lot of 65)

**AMENDED 2026-05-16** per Agent C deep-research finding (Rule 11 STT
Rules + Section 170 CGST + Indian Stamp Rules 2019). STT, stamp duty,
and GST are STATUTORILY rounded to nearest rupee (not paise). Brokerage,
exchange, SEBI, IPFT remain paise-precision. See `COSTS_VERIFIED.md`
§3 for citations.

Buy premium notional: 65 × ₹100 = **₹6,500**.
Sell premium notional: 65 × ₹120 = **₹7,800**.
Gross PnL: **+₹1,300**.

| Component | Buy side | Sell side | Total | Rule applied |
|---|---|---|---|---|
| Premium × lot | -6,500.00 | +7,800.00 | +1,300.00 | gross |
| Brokerage (₹20 flat per order) | -20.00 | -20.00 | -40.00 | paise |
| STT 0.15% × ₹7,800 = ₹11.70 | 0 | — | **-12** | **round to nearest rupee (Rule 11)** |
| Exchange NSE (0.03503%) | -2.28 | -2.73 | -5.01 | paise |
| SEBI (0.0001%) | -0.01 | -0.01 | -0.02 | paise (per-side) |
| IPFT NSE (0.0005%) | -0.03 | -0.04 | -0.07 | paise (per-side) |
| Stamp duty 0.003% × ₹6,500 = ₹0.195 | -0 | 0 | **-0** | **round to rupee (<0.50 → 0)** |
| GST base = 40 + 5.01 + 0.02 + 0.07 = ₹45.10 | | | | |
| CGST 9% × 45.10 = ₹4.059 | -2 | -2 | -4 | **round to rupee (Section 170)** |
| SGST 9% × 45.10 = ₹4.059 | -2 | -2 | -4 | **round to rupee (Section 170)** |
| **Charges total** | | | **-65.10** | |
| **Net PnL** | | | **+1,234.90** | gross 1,300 − 65.10 |

**Round-trip cost = ₹65.10 = 1.00% of buy premium / 5.0% of gross move.**

Critical note: multi-lot trades change the per-lot cost ratio drastically
because brokerage is flat per ORDER, not per lot. See `COSTS_VERIFIED.md`
§11 for multi-lot examples.

For a deeper OTM trade (₹50 → ₹60, 1 lot, post-Apr-2026):
- Buy: 65 × 50 = ₹3,250. Sell: 65 × 60 = ₹3,900. Gross +₹650.
- Charges ≈ ₹58 = 1.78% of buy premium / 8.9% of gross move.
- **Minimum profitable move on deep OTM ≈ 1.78% on premium just to break even on costs alone** (excludes slippage).

### 8.3 Lot size history (PER-DATE LOOKUP REQUIRED)

Lot sizes changed 7 times in our 2021-2026 backfill window. Using a
single lot would mis-size positions by 3-7× in places. Build
`lot_size_history.csv` keyed on `(instrument, effective_date)` —
REQUIRED, not optional.

| Effective from | NIFTY 50 | BANKNIFTY | SENSEX | Source |
|---|---|---|---|---|
| ≤ Jun 2021 | 75 | 25 | 10 | NSE historic |
| **Jul 2021 expiry** | 50 | 25 | 10 | NSE bulletin |
| **30-Jun-2023** | 50 | 15 | 10 | Zerodha Marketintel #353422 |
| **26-Apr-2024** | 25 | 15 | 10 | NSE FAOP circular |
| **20-Nov-2024 (new contracts)** | 75 | 30 | 20 | SEBI/HO/MRD-PoD2/CIR/P/2024/00181 — ₹15L notional minimum |
| **Jul 2025 (transitional)** | 75 | 35 | 20 | NSE bulletins |
| **30-Dec-2025 EOD** | 75 | 30 | 20 | NSE FAOP70616 |
| **Jan 2026 onwards (CURRENT)** | **65** | **30** | **20** | NSE Jan-2026 circular — notional rebased on Sep-25 closing prices, ₹15-20L range |

Source-cites:
- sahi.com 2026 lots
- Ventura Securities Jan 2026 blog
- Scanx NSE Jan 2026 circular
- PL Capital NIFTY lot history
- Zerodha Marketintel bulletins

### 8.4 Constants to bake into code

When `brutex/costs/dhan_constants.py` lands (Phase 3 deliverable):

```python
# Verified 2026-05-16, see TRACK2_OPTIONS_SPEC.md §8.1 for source-cites.

DHAN_BROKERAGE_INR_PER_ORDER     = 20.00
STT_OPTIONS_SELL_PCT_PRE_APR2026 = 0.0010   # 0.10%
STT_OPTIONS_SELL_PCT_POST_APR2026= 0.0015   # 0.15% (Budget 2026)
STT_HIKE_EFFECTIVE_DATE          = date(2026, 4, 1)
EXCHANGE_NSE_FNO_PCT             = 0.00035030
EXCHANGE_BSE_FNO_PCT             = 0.00032500
SEBI_TURNOVER_PCT                = 0.0000010
IPFT_NSE_PCT                     = 0.0000050
STAMP_DUTY_BUY_PCT               = 0.00003   # 0.003%
GST_PCT                          = 0.18      # on brokerage + exchange + SEBI + IPFT only
TICK_SIZE_INR_INDEX_OPTIONS      = 0.05      # 5 paise — unchanged for index options
```

---

## 9. Slippage model (LOCKED 2026-05-16, delta-bucketed)

Per agent research 2026-05-16: industry-magnitude estimates from PL
Capital, Groww, Journal of Asset Management 2024 (DOTM bid-ask
amplification study), Bajaj Broking. **Final calibration must be done
from operator's own Dhan tick history once data ingest is operational
(Phase 2).**

### 9.1 Per-side slippage by delta bucket

| Delta range | Strike position | Per-side slippage | Rationale |
|---|---|---|---|
| 0.45 - 0.55 | ATM | **1 tick = ₹0.05** | typical spread 5-15 paise on NIFTY ATM peak hours |
| 0.20 - 0.44 | OTM 1-3 strikes | **2 ticks = ₹0.10** | spread 15-50 paise |
| 0.05 - 0.19 | OTM 4-8 strikes | **5 ticks = ₹0.25 OR 0.5% of mid, whichever greater** | spread 50 paise to ₹2 |
| < 0.05 | Deep OTM | **EXCLUDE from backtest universe** | spread routinely > 5% of premium; results = noise |

### 9.2 Time-of-day multiplier

| Window (IST) | Multiplier | Why |
|---|---|---|
| 09:15 - 09:30 | **× 1.5** | open auction settling, wide spreads |
| 10:00 - 14:30 | **× 1.0** | peak liquidity baseline |
| 09:30 - 10:00, 14:30 - 15:00 | **× 1.25** | transitional |
| 15:00 - 15:20 | **× 1.5** | pre-close, wider spreads + thin book |

### 9.3 Volatility multiplier (India VIX-driven)

| India VIX | Multiplier | Source |
|---|---|---|
| VIX ≤ 20 | × 1.0 | baseline |
| 20 < VIX ≤ 30 | × 1.5 | engineering default (no academic cite) |
| VIX > 30 | × 2.0 | event-day, very wide spreads |

India VIX series already ingested via Track 1 (`securityId=21`, see Track 1 spec §Data Source).

### 9.4 Final per-side slippage formula

```
slippage_inr = base_slippage(delta_bucket) × tod_multiplier × vix_multiplier
```

Applied separately on entry fill (worst price within entry bar + slippage)
and exit fill (same).

### 9.5 Sweep cardinality (Phase 4)

Old §9 (TBD draft) proposed 5 × 4 × 4 = 80 slippage profiles. New
locked design uses **delta-bucketed deterministic** slippage (NOT a
brute-force grid) since the delta bucket is determined by strike
position relative to ATM and is not a free parameter. **Sweep
cardinality reduces from 80 to 1 slippage profile per (signal, strike,
expiry).** Variance comes from delta bucketing + time-of-day + VIX
naturally.

---

## 8b. UNVERIFIED items (require operator manual verification before code-lock)

Per CLAUDE.md Golden Rule #1, items the research agent could not verify
via automated fetch:

1. **Dhan official rate card** (`dhan.co/pricing`, `dhan.co/charges`):
   every URL returned HTTP 403 to WebFetch. Numbers in §8.1 are from
   3+ secondary sources (Zerodha, Angel One, Chittorgarh, IPOGMPINDIA)
   that quote Dhan directly. **Action: operator manually visits the
   Dhan page in browser + posts a screenshot. If any number differs,
   amend §8.1 before code-lock.**

2. **STT 0.15% post-1-Apr-2026**: Budget 2026 proposed the hike from
   0.10% to 0.15%. Multiple sources state it became effective
   1-Apr-2026, but the agent did not pull the final Finance Act 2026
   gazette text. **Action: operator verifies the Finance Act 2026 (or
   gazette notification) before code-lock — could still be 0.10% if
   the proposal was modified.**

3. **BSE Bankex options exchange charge**: conflicting sources —
   ₹3,250/cr (0.03250%) per one source, ₹325/cr (0.00325%) per another
   (Ventura). **Action: verify directly from BSE Membership Transaction
   Fees page before code-lock.** §8.1 currently uses 0.03250% (Angel
   One verified).

4. **Dhan plan tiers** (Free / Founder / Pro): no evidence found in
   2026 sources. Only documented variant is a **50% brokerage discount
   for women customers** (₹10/order). DhanHQ refers to the API access
   product, not a brokerage tier. **Action: confirm with operator
   account.**

5. **Slippage paisa-level calibration**: agent's numbers in §9.1 are
   industry-magnitude estimates, NOT measured from Dhan tick data.
   **Action: final calibration from operator's own Dhan tick history
   in Phase 2 data ingest.**

6. **NIFTY index-option tick size at 5 paise**: NSE reduced
   cash-segment tick for sub-₹250 stocks to 1 paise (May 2024) and
   stock-options to ~1 paise (Nov 2025). **Index options (NIFTY,
   BANKNIFTY) remain 5 paise** per agent research — but re-verify
   before code-lock since regulator may extend the 1-paise rule to
   index options.

These items are blockers for Phase 3 code-lock but NOT blockers for
Phase 2 data-ingest. Phase 2 can start as soon as operator provides
Dhan access token; cost-model verification can run in parallel.

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

> **Chunk count uses `ceil(window_days / 30)`** — a partial leftover
> window still needs one call. Reality-check agent flagged the initial
> floor-based count as off-by-one.

| Quantity | Calculation | Value |
|---|---|---|
| Calendar days in window | 2021-01-01 → 2026-05-15 | ~1,960 |
| Chunks per series (30-day chunks, ceil) | Per-stream window / 30, rounded up | NIFTY: 66 / BN-WEEK: 48 / BN-MONTH: 66 / SENSEX: 66 |
| Calls — NIFTY (2 streams) | 42 × 66 × 2 | 5,544 |
| Calls — BANKNIFTY-WEEK | 42 × 48 | 2,016 |
| Calls — BANKNIFTY-MONTH | 42 × 66 | 2,772 |
| Calls — SENSEX (2 streams) | 42 × 66 × 2 | 5,544 |
| **Total backfill calls** | | **~15,876** |
| Effective rate (3 r/s cap) | adapter shared token bucket | 3 calls/sec |
| Backfill wall time | 15,876 / 3 = 5,292 sec | **~88 minutes** |
| Daily quota used | 15,876 / 100,000 | **15.9%** (one-time, below cap) |

### Storage

> **Trading-day note:** the 2021-01-01 → 2026-05-15 window is **5.37
> years**, not 5.0. At ~250 trading days/year that's ~1,342 trading
> days (not the prior 1,250). Reality-check agent corrected this.

| Quantity | Value |
|---|---|
| Bars per trading day | 375 (09:15–15:30 IST, 1-minute) |
| Bytes per bar (Parquet ZSTD, 16 columns) | ~50 (uncompressed ~128 bytes; ZSTD ~2.5× ratio) |
| Bytes per series (full 5.37y, ~1,342 td) | 375 × 1,342 × 50 = 25.2 MB |
| Bytes per series (BN-WEEK active ~3.87y, ~966 td) | 375 × 966 × 50 = 18.1 MB |
| Bytes per series (SENSEX 2021→today, ~1,342 td but pre-2023-05-15 thin) | (375 × 642 × 50 × ~0.3 fill) + (375 × 700 × 50) = 3.6 + 13.1 = 16.7 MB |
| Total: NIFTY 2 streams × 42 series × 25.2 MB | 2.12 GB |
| Total: BANKNIFTY WEEK 42 × 18.1 MB | 0.76 GB |
| Total: BANKNIFTY MONTH 42 × 25.2 MB | 1.06 GB |
| Total: SENSEX 2 streams × 42 series × 16.7 MB | 1.40 GB |
| **Total options lake (5.37y, 252 series)** | **~5.5 GB** |

### Daily incremental (post-backfill)

| Quantity | Value |
|---|---|
| Active series on a given day (post-2024-11-13, BN-WEEK dormant) | 210 (252 minus 42 dormant BN-WEEK) |
| Active series on a historical day (2021-01-01 → 2024-11-13) | 252 (all streams live) |
| Calls per day (post-2024-11-13) | 210 |
| Daily quota used | 210 / 100,000 = **0.21%** |
| Daily wall time | 210 / 3 = 70 sec |

Total disk: Track 1 spot lake (~80 MB) + Track 2 options lake (~5.5 GB)
≈ 5.6 GB. Compute lake (results.parquet) is the dominant disk user.

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

Same audit log as Track 1. Track 2 adds these audit kinds (vocabulary
`drvOptionType` ∈ `{CALL, PUT}` per Dhan SDK — NOT `CE`/`PE`):

| audit_kind | Payload (compact) |
|---|---|
| `OPT_INGEST_CHUNK_OK` | (underlying, cycle, strikePosition, drvOptionType, from, to, n_bars, n_NaN, raw_bar_hash_aggregate, cooked_bar_hash_aggregate, bsm_kernel_version) |
| `OPT_INGEST_CHUNK_ERR` | (underlying, cycle, strikePosition, drvOptionType, from, to, error_code, retry_count) |
| `OPT_GREEK_COMPUTED` | (underlying, cycle, strikePosition, drvOptionType, bar_ts, IV, delta, gamma, theta, vega, bsm_kernel_version) — sampled (1 per 1000 bars) |
| `OPT_BACKTEST_ENTRY` | (signal_hash, underlying, strike, drvOptionType, entry_premium_paisa, slippage_profile_hash) |
| `OPT_BACKTEST_EXIT` | (signal_hash, exit_reason, exit_premium_paisa, pnl_paisa) |

Audit chain SHA256 continues from Track 1 — never resets.

**Concurrency-determinism rule:** audit appends are serialized through a
single asyncio writer task (per `TRACK2_DATA_INGEST_DESIGN.md §13`). flock
provides cross-process safety only. Replay oracle O-O1 sorts replayed
audit rows by `(stream, chunk_from)` to remove asyncio-scheduler
nondeterminism.

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
| L2 property | 26 | **+12 options-specific** | 38 |
| L3 integration / oracle | 4 | **+2** (options replay + BSM-vs-py_vollib) | 6 |
| L4 golden / fixtures | yes | options replay golden | yes |
| L5 contract | yes | rollingoption contract + expiryCode historical semantics + withdrawn-Monday-window probe | yes |
| L6 audit-chain | yes | options audit kinds; raw + cooked bar_hash | yes |
| L7 bench (O(1)) | yes | options bar lookup; Greeks compute | yes |
| L8 stress | yes | 24h options replay | yes |
| L9 dashboard / smoke | yes | options health gate (C12) | yes |

**12 Track 2-specific property invariants:**

| ID | Invariant |
|---|---|
| P-O1 | ATM-rolled rollingoption series has monotonic timestamps within each contract span |
| P-O2 | Premium ≥ 0 always; volume ≥ 0; OI ≥ 0 |
| P-O3 | OHLC consistency: `low ≤ open, close ≤ high` |
| P-O4 | Strike step = published step (50 for NIFTY, 100 for BANKNIFTY/SENSEX) — verified historically against Dhan master CSV snapshots |
| P-O5 | strikePosition ∈ [-10, +10] |
| P-O6 | At expiry-day 15:30 IST, time-to-expiry T ≤ 1/(365.25 × 86400); BSM uses clamp(T, 1 min) per §13 expiry-day theta policy |
| P-O7 | Delta(CALL) ∈ [0, 1]; Delta(PUT) ∈ [-1, 0] |
| P-O8 | Gamma ≥ 0 always (call and put) |
| P-O9 | Theta ≤ 0 always (long premium decays) |
| P-O10 | Put-call parity: \|C − P − (S − K·e^{-rT})\| ≤ `max(₹0.10, 0.1% × min(C,P))`; **skipped** when C<₹0.50 or P<₹0.50 or either is NaN (handles thin SENSEX pre-2023-05-15 window without false RED) |
| P-O11 | Audit chain unbroken across mixed Track1+Track2 ingest |
| P-O12 | `strike_api == strike_computed` modulo Dhan tie-break at exact midpoints (`expected_tiebreak_table[underlying]` lookup; deviations must match recorded tie-break rule) |

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
- #210: Storage estimate ~5.5 GB for 5.37y window (revised after S1 verification + reality-check trading-day correction)
- #211: Backfill ~15,876 calls / ~88 min wall time / 15.9% daily quota (revised — chunk count uses ceil; reality-check off-by-one correction)
- #211a: Two-hash bar integrity — `raw_bar_hash` (Dhan-returned OHLC+IV+spot+strike_api) + `cooked_bar_hash` (raw_bar_hash ‖ Greeks ‖ BSM_KERNEL_VERSION); audit log carries both; replay oracle O-O1 checks both (hostile-review fix)
- #211b: CALL/PUT vocabulary locked everywhere (SPEC, INGEST, audit, watermark keys, folder names) — never CE/PE (matches Dhan SDK `drvOptionType` validator)
- #211c: Lot size stored in date-keyed `reference/lot_size_history.parquet`; `volume`/`open_interest` columns are contract counts (lot-size-independent) — handles SEBI lot-size revisions across the 5y window
- #211d: Audit log appends serialized through single asyncio writer; replay sorts by `(stream, chunk_from)` for asyncio-scheduler determinism
- #211e: Risk-free rate lookup is step-function (no interpolation) at monthly boundaries
- #211f: Expiry-day Greeks clamp T at 1 min to avoid theta divergence at 15:29-15:30 bars
- #211g: `interval=1` integer (not string) per Dhan SDK; CI lint catches `interval='1'` regression
- #211h: **ATM tie-break rule** — Phase 2 ingests a calibration batch (≥1 month, all 6 streams) and observes Dhan's tie-break behavior at exact spot-midpoint cases. The empirically-observed rule is locked into `EXPECTED_TIEBREAKS[underlying]` and verified by invariant P-O12. Operator signs off on the resulting table before Phase 2 backfill proceeds. (Sensible default if no midpoint case is observed: `round-half-up`.)
- #211i: **Token refresh race protection** — single shared `asyncio.Lock` around the auth-renewal call. The first task to hit HTTP 401 acquires the lock, refreshes, then releases; all other 251 tasks waiting at the rate-limiter resume with the new token automatically (token is read from a shared `TokenCell`, not passed per-task). Prevents thundering-herd refresh.
- #212: Cost model lives at `brutex/options/costs.py`, version-stamped
- #213: Slippage profile = 5 × 4 × 4 = 80 combos per (signal, strike, expiry)
- #214: Money is int64 paisa (same as Track 1)
- #215: Audit kinds OPT_INGEST_*/OPT_GREEK_*/OPT_BACKTEST_* added
- #216: 12 Track 2 property invariants added (Phase 1; P-O1..P-O12)
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

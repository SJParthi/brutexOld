# COMMON Shared Options-Greeks Engine — Implementation Plan

> **Status: PLAN + PROPOSED ADR (docs only). NO code. Awaiting operator go
> before Phase 1.**
>
> **Working decision ID:** `DEC-GREEKS-SHARED-CORE-001`
> (MIGRATION.md entry #1070; ADR draft `docs/adr/0015-shared-greeks-engine.md`,
> status PROPOSED), REFINED by `DEC-GREEKS-GENERATION-TIME-001` (MIGRATION.md
> entry #1071; §15 below — the generation-time compute-and-store refinement of
> the storage decision). This plan REVERSES the DEC-P0-025 Greeks deletion, scoped
> to the Track-2 options-realism leg + the lake read-enrichment. The
> brute-force **ENGINE stays NIFTY / BANKNIFTY / SENSEX** (CLAUDE.md §3) — Greeks
> are lake / REFERENCE_ONLY, never a 60-condition vocabulary input.
>
> Every codebase `file:line` citation in this plan was re-verified against the
> repository HEAD in the deep-research session that produced it (git HEAD
> `f7c2f81b`, re-confirmed on `origin/main`). Every external / math claim cites
> a standard source or is marked UNVERIFIED (Golden Rule #1 / #10).

---

## 0. Overview — what Greeks are (plain English AND technical)

**Plain English.** An option's price moves for several distinct reasons: the
underlying moving, time passing, and the market's fear/greed (volatility)
changing. "The Greeks" are the five numbers that quantify *how much* the option
price moves per unit of each of those causes. They are the standard risk
dashboard every options desk reads:

| Greek | Plain English | Technical | Units |
|---|---|---|---|
| **Delta (Δ)** | How much the option gains per ₹1 the underlying rises | ∂V/∂S | signed, CE ∈ (0,1), PE ∈ (−1,0) |
| **Gamma (Γ)** | How fast Delta itself changes | ∂²V/∂S² | ≥ 0 |
| **Theta (Θ)** | How much value bleeds away per day (time decay) | ∂V/∂t | ≤ 0 for long premium (per calendar day) |
| **Vega (ν)** | How much the price moves per 1% change in implied volatility | ∂V/∂σ | ≥ 0 (quoted per 1% IV) |
| **Rho (ρ)** | How much the price moves per 1% change in the risk-free rate | ∂V/∂r | signed |
| **IV (σ)** | The volatility the market's price *implies* — the input that reproduces the traded price | Black-76 inverse | ≥ 0, clamped |

**Technical.** For a European option under the Black-Scholes-Merton /
Black-76 framing (Hull, *Options, Futures, and Other Derivatives*, 11e,
ch. 15/17/19), the price and every Greek are a fixed closed form of
`(F, K, T, r, σ, is_call)` where `F` is the forward, `K` the strike, `T` the
time-to-expiry in years, `r` the risk-free rate and `σ` the volatility. Given
a traded price and everything but `σ`, `σ` is recovered by numerically
inverting the pricing formula ("implied volatility"). This plan builds ONE
shared Rust closed-form kernel + IV inverter, consumed bit-identically by
BruteX (backtest) and TickVault (live).

---

## 1. Ground truth (verified, not assumed)

There is **NO Greeks / IV / Black-Scholes code anywhere in BruteX today.** This
is not an oversight — it was deliberately deleted, and it is recoverable. The
plan starts from this honest baseline:

| Claim | Verified evidence |
|---|---|
| No Greeks / IV / BSM code exists | `ls brutex/options/greeks* risk_free*` → no such file; `brutex/options/__init__.py:19-20` lists both as `(TBD)` |
| Rust kernel deleted in PR #47 / DEC-P0-025 | `git log a87bce43` = "prune fno-math … remove Greeks (DEC-P0-025, PR #47)"; the removal commit is real |
| Kernel recoverable from git | `git show a87bce43^:rust/fno-math/src/greeks/black_scholes.rs` = **2410 lines**; the directory holds 8 files |
| Both IV seams already existed | the deleted `black_scholes.rs` exposes `compute_greeks(...)` at `:272` (Newton solve from price) **and** `compute_greeks_from_iv(...)` at `:312` (price off a given IV) — the exact live-vs-solved seam |
| Lake stores NO IV / NO spot on the option bar | `brutex/data/parquet_writer.py` schema = OHLCV + OI only; `brutex/options/lake_source.py:215-216` hard-codes `spot=None, iv=None` |
| Groww historical payload = 7 elements | `[ts, open, high, low, close, volume, open_interest]` — `docs/groww/api-reference/03-backtesting.md:202-236`; there is NO `iv` field |
| Per-bar spot join already built | `brutex/dashboard/db_view.py:2047` `_underlying_spot_close_by_ts`, `:2140` `_fno_derived_rows` (moneyness / spot / intrinsic / time_value / days_to_expiry) — DEC-FNO-MONEYNESS-001 / DEC-DB-FNO-COLUMNS-001 |
| Delta-target consumer already waiting | `brutex/options/strike_rules.py:190` `resolve_delta_target_strike`; `executor.py:401-406` raises "the Greeks pipeline is Phase 4" when a `DELTA_TARGET` rule is given without a `delta_chain` |
| Bit-identity precedent (f64) | `rust/trading-core/tests/ema_golden.rs:31-56` = bit-for-bit `assert_eq!` on `Vec<Option<f64>>`; `tests/integration/test_python_rust_parity.py` |
| Deleted code was calibrated to the retired vendor | `historical_rates.rs` carried an RBI table + a fixed `r = 0.10`; `iv_solve` converted spot→forward before calling `jaeckel::implied_black_volatility` |

**Load-bearing consequence — a STALE spec premise.** The Track-2 spec
(`docs/track2/TRACK2_OPTIONS_SPEC.md` §7.1 / §7.7) says "IV comes from the
vendor, authoritative, with a 50 bp cross-check". That was written before
ADR-0009 for the *retired* vendor's rolling-option endpoint, which returned an
`iv` array. **The current Groww historical candle endpoint returns none** (7
elements, no `iv` — verified above). Therefore **IV must be SOLVED from the
traded option price**, and the numerical inversion becomes the PRIMARY IV
source, not a cross-check. This spec drift is the single most important
decision and is reconciled by the MIGRATION.md #1070 entry + the ADR BEFORE any
code lands (Golden Rule #2 / #3).

---

## 2. The math / models

### 2.1 Model family — Black-76 on the forward (per instrument)

For a European option, the market-standard precise framing is **Black-76 on
the forward** `F = S · e^((r − q)·T)`, which is **algebraically identical** to
BSM-with-`q` for European options (Hull 11e, ch. 18; TRACK2_OPTIONS_SPEC.md
§7.10). The kernel is named Black-76 to be explicit about `F` and to match the
IV inversion, which operates in forward / Black space (the deleted `iv_solve`
already converted spot→forward before calling the Jäckel inverter).

Per-instrument model selection (the "right model per instrument" requirement):

| Underlying kind | Exercise style | Model | Greeks status |
|---|---|---|---|
| NIFTY 50 / BANKNIFTY / SENSEX index options | European, cash-settled (spec §7.10) | Black-76 closed form | **EXACT** |
| ~212 single-stock F&O options (REFERENCE_ONLY) | **American** (early exercise + discrete dividends) | closed-form is NOT exact | **FLAGGED APPROX or NA** (Proposed Decision #1) |
| FUT legs | n/a | n/a | **NA** (`—`, like moneyness for a future) |

Exercise style is derived from the underlying's `instrument_type` in the
instrument master (`IDX` → European, `EQ` → American), **NOT** from the F&O
symbol (`parse_fno_symbol` carries no exercise-style field —
`brutex/data/fno_symbol.py:60-93`). An UNKNOWN underlying defaults to the
conservative branch (NA / flagged), never silently European.

> **HONESTY (UNVERIFIED, cite-honest):** the European / cash-settled claim for
> the 3 indices is spec-ASSERTED (TRACK2_OPTIONS_SPEC.md §7.10, citing NSE
> Clearing / BSE contract pages) but was NOT independently re-verified in the
> read-only research session. Re-confirm against the live NSE / BSE contract
> specifications before Phase 2 coding.

### 2.2 Closed-form Greeks (the formulas to author)

The deleted `OptionGreeks` struct carried rho plus the full 2nd/3rd-order set
(charm / vanna / volga / veta / speed / color / zomma / ultima) plus
`bs_price` / `intrinsic` / `extrinsic`. **Scope the revived kernel to the six
the spec + the delta-consumer need: `iv, delta, gamma, theta, vega` + `rho`**
(rho is cheap and is the only r-sensitive Greek — include it since `r` is now
first-class). Standard Black-76 forms, with
`d1 = [ln(F/K) + σ²T/2] / (σ√T)`, `d2 = d1 − σ√T`, discount factor `e^(−rT)`
(Hull 11e ch. 17/19):

- **delta** — signed: CE `e^(−rT)·N(d1)` ∈ (0,1); PE `−e^(−rT)·N(−d1)` ∈ (−1,0)
- **gamma** — `e^(−rT)·φ(d1) / (F·σ·√T)`, ≥ 0, identical for CE / PE
- **vega** — `F·e^(−rT)·φ(d1)·√T`, ≥ 0, identical for CE / PE (quoted per 1% IV = value/100)
- **theta** — per calendar day, ≤ 0 for long premium; standard Black-76 form
- **rho** — CE `K·T·e^(−rT)·N(d2)`; PE `−K·T·e^(−rT)·N(−d2)`

`N` = standard normal CDF (via `erf`); `φ` = standard normal PDF. These
transcendentals (`exp / ln / sqrt / erf`) are the cross-platform determinism
risk of §7.

### 2.3 IV solve — the one-optional-sigma seam

The single shared entry point is one pure stateless function with an OPTIONAL
sigma:

```
greeks(price: Option<f64>, sigma: Option<f64>, forward: f64, strike: f64,
       t_years: f64, rate: f64, is_call: bool) -> Result<GreeksOut, String>
```

- `sigma = Some` — TickVault live / a future vendor IV → skip the solve, price
  Greeks directly (the deleted `compute_greeks_from_iv` path, verified at
  `black_scholes.rs:312`).
- `sigma = None` — BruteX historical → solve IV from `price` first (the deleted
  `compute_greeks` path, verified at `black_scholes.rs:272`), then price the six
  Greeks off the SAME closed form.

Both paths converge on ONE Greek kernel — this single optional-sigma seam is
precisely how live and backtest share one core.

**Solver = Jäckel "Let's Be Rational"** (Peter Jäckel, *"Let's Be Rational"*,
2015 — a bounded, ~2-Householder-iteration, machine-precision, O(1) inversion),
NOT raw Newton (Newton diverges as vega→0 deep OTM / ITM). Sourcing is
Proposed Decision #5 / Open Decision #7: either re-add the pinned `jaeckel`
crate (if the `cargo-deny` / edition gate passes) or vendor a small
deterministic Rational + bisection fallback guarded by the admission floors.
`py_vollib` and QuantLib remain CI oracles ONLY, never the runtime path
(COMMON, Golden Rule #11).

---

## 3. The exact input set (available vs solved)

Per historical option bar the Black-76 inputs are:

| Input | Availability | Source |
|---|---|---|
| Option premium `P` | **AVAILABLE** | stored `close` (or OHLC) — the price to invert |
| Strike `K` | **AVAILABLE** | `parse_fno_symbol` |
| Option type CE / PE | **AVAILABLE** | `parse_fno_symbol` |
| Underlying spot `S` | **AVAILABLE (join)** | reuse `_underlying_spot_close_by_ts` per-bar join — zero extra read |
| Time-to-expiry `T` | **DERIVABLE** | (expiry-date @ 15:30 IST − bar instant) / 365.25 / 86400 — NOT the calendar-day `days_to_expiry` int |
| Dividend yield `q` | **ASSUMED** | `q = 0` for indices (Proposed Decision #3, documented bias — §7.4); larger / discrete for stocks (reinforces stock = approx) |
| Risk-free `r` | **MISSING** | a NEW risk-free source is a Phase-1 prerequisite (Proposed Decision #2) |
| Volatility `σ` (IV) | **MISSING → SOLVED** | inverted from `P` via Jäckel — the pipeline's core |

Everything is available EXCEPT `r` and `σ` (and the numeric kernel). `σ` is
solved; `r` is the one external data dependency to source in Phase 1.

---

## 4. Per-bar derivation (the read path)

Greeks are **read-time derived, NEVER stored** in the source-of-truth path
(they are time-varying and depend on the `r` / `q` choices — the same reasoning
as moneyness, which the F&O column schema documents at
`fno_candle_lake.py:118-121`). The derivation EXTENDS `_fno_derived_rows`
(`db_view.py:2140`), which ALREADY:

1. does ONE per-page underlying-spot join (`_underlying_spot_close_by_ts`) → gives `S` per bar (O(page) build, O(1)/bar lookup),
2. decodes `K`, expiry, option_type per bar,
3. computes intrinsic / time_value / days_to_expiry.

The Greeks step adds, per bar and off that SAME join: build `F = S·e^((r−q)T)`,
solve `σ` from `close`, price `delta / gamma / theta / vega / rho`. **No new
I/O.** Then append `iv, delta, gamma, theta, vega, rho` to `FNO_DERIVED_COLUMNS`
as `FnoCandleColumn(source="derived")`.

Optional pull-time materialization is covered in §6 ("When to compute"); the
recommended shape is HYBRID — read-time is the source of truth, pull-time is an
optional cache using the SAME kernel so results are bit-identical.

> **REFINEMENT (§15, `DEC-GREEKS-GENERATION-TIME-001`).** §15 refines this
> read-path stance on a COST-CLASS basis: the CHEAP family (`moneyness`, `spot`,
> `intrinsic`, `time_value`, `days_to_expiry`) stays read-time derived exactly as
> above, but the EXPENSIVE family (`iv`, `delta`, `gamma`, `theta`, `vega`, `rho`
> — each carrying a per-bar IV root-solve) is COMPUTED-AND-STORED at generation
> time via a post-pull `greeks_enrich_after_pull` hook (the F&O twin of
> `_resample_daily_after_pull`). §15 supersedes the "NEVER stored" wording of this
> section for that expensive family only; the cheap-family reasoning here is
> UNCHANGED. §15 is CONDITIONAL and gated (it cannot ship until the shared kernel
> + frozen golden land and `r`/`q`/`T` are locked); this §4 read-time path remains
> the honest NULL-fallback for any bar without a stored Greek.

---

## 5. Where Greeks surface (dependency order)

Every surface below is READ-ONLY. The 52→60-condition vocabulary and the
brute-force engine are UNTOUCHED (NIFTY / BANKNIFTY / SENSEX, CLAUDE.md §3).

1. **`/db` F&O candle table** (primary — the operator's ask). Extend
   `_fno_derived_rows` (`db_view.py:2140`); advertise the six columns in
   `FNO_CANDLE_COLUMNS`. The grid renders `source="derived"` columns
   generically (precedent DEC-DB-FNO-COLUMNS-001 #881), exactly like the
   moneyness / intrinsic / time_value / days_to_expiry columns already shown.
2. **`/movers` options screen.** `OptionMover` already carries moneyness from
   the same as-of snapshot join (`movers_options.py`); add `delta` / `iv` cells
   to `compute_option_mover` — no new read (reuses the HLC3 read of
   DEC-MOVERS-OPTIONS-VALUE-DEDUP-001 #903).
3. **Options executor (backtest realism).** Wire a lake-backed
   `DeltaChainProvider` (the executor already accepts one, `executor.py:587`) to
   feed `resolve_delta_target_strike`, activating the dormant delta-target
   strike rules 8 / 9 / 10. Stamp `iv_at_entry` from the solve (`executor.py:498`
   currently reads `pair.entry_bar.iv`, always `None` today).
4. **TickVault live** (contract only, from this repo). The shared Rust `greeks`
   kernel + the frozen `greeks_golden.json` are the bit-identity contract; the
   live path feeds `sigma = Some` (vendor / live IV). TickVault-side adoption is
   out-of-repo (Open Decision) and CANNOT be proven from BruteX (Golden Rule #10).

---

## 6. When to compute — read-time vs pull-time materialization vs hybrid

There are three viable placements. **All three call the SAME Rust core, so any
computed value is bit-identical regardless of when it runs** — the choice is
purely *when* the identical arithmetic executes, never *what* it produces.

| Mode | What it means | Pros | Cons |
|---|---|---|---|
| **Read-time (default, flexible)** | Greeks derived on every `/db` / `/movers` read off the existing per-bar spot join, never stored | Zero storage; the `r` / `q` choice is a live parameter (change `r` → re-read, no re-pull); mirrors moneyness exactly; no lake-schema change | Re-computed each read (but O(1)/bar over an already-bounded page — negligible) |
| **Pull-time materialization (stored, faster)** | Greeks computed once at pull time and written to Parquet columns | A cold read is a plain column read | A lake-SCHEMA change (REFERENCE_ONLY-scoped); freezes the `r` / `q` choice into stored bytes; a changed rate needs a re-materialize; time-varying values baked in — the exact reason moneyness is NOT stored |
| **HYBRID (recommended)** | Read-time is the SOURCE OF TRUTH; pull-time is an OPTIONAL cache using the identical kernel | Flexible + fast; the cache is a pure accelerator that can be dropped/rebuilt with zero correctness impact | Slightly more moving parts (one cache to invalidate on an `r` / `q` change) |

**Recommendation: HYBRID.** Ship read-time first (Phase 3a) as the definitive,
flexible, schema-free source of truth. If a cold read ever proves slow on a
deep partition, ADD an optional pull-time materialized cache LATER as a pure
accelerator (its own separately-scoped decision) — never as the source of truth,
and always regenerable from the read-time kernel. Because both paths are the
one Rust core, a materialized value and a read-time value are provably
identical (the frozen golden is the contract). This resolves Open Decision #8
(spec §7.9 once said "precomputed and stored in Parquet"; the recommendation is
derive-first, optional-cache-later).

> **REFINEMENT (§15, `DEC-GREEKS-GENERATION-TIME-001`).** §15 SHARPENS this
> "derive-first, optional-cache-later" HYBRID into a HYBRID DRAWN ON COST-CLASS:
> the EXPENSIVE family (each Greek carries an IV root-solve, an unbounded per-read
> cost on immutable past bars) is COMPUTED-AND-STORED at generation time (the
> stored bytes are the fast path, the read-time §4 kernel the NULL-fallback), while
> the CHEAP family (trivial O(1) moneyness arithmetic) stays read-time derived. The
> "pull-time is only a droppable cache" framing above is upgraded to "for the
> expensive family, the stored column IS the served value (with a read-time
> fallback), because re-solving IV on every read forever is the waste store-once
> eliminates" — but the store remains regenerable from the same kernel, so the
> reversibility argument holds. §15 is CONDITIONAL/gated; until it ships, this §6
> read-time-first HYBRID is the live plan.

---

## 7. Proposed default decisions (sensible defaults — all operator-overridable)

These are the RECOMMENDED starting positions. Each is a default, not a lock —
the operator may override any of them, and the ADR records the choice.

| # | Decision | Proposed default | One-line rationale |
|---|---|---|---|
| 1 | American single-stock options | **FLAGGED APPROXIMATION** (`american_style_approx`), NOT NA | So the ~212 stocks still get *usable* Greeks; the flag keeps it honest that closed-form is inexact for American exercise. |
| 2 | Risk-free rate `r` | **A configurable FIXED rate (~6.5%) to start**; an RBI / T-bill curve as a follow-up | Reproducible + simple + matches NSE's own fixed-rate option-chain convention; the versioned curve is accuracy polish (rho sensitivity ≈ 1.3% weekly per 100 bp — a bias, not a blowup). |
| 3 | Dividend yield `q` | **`q = 0`** with the documented bias | Simplest, reproducible; the bias direction is stamped on every output (over-prices calls / under-prices puts by ~0.004% daily at NIFTY's trailing yield). |
| 4 | Time-to-expiry `T` | **NSE trading-minutes** (business-time) | More precise near expiry, where calendar Act/365.25 over-counts weekend/holiday decay; `session_minutes_per_day() = 375` already exists (`nse.py`). |
| 5 | IV solver | **Jäckel "Let's Be Rational"** (pinned / vendored) | Bounded, machine-precision, O(1); Newton diverges deep OTM / ITM where vega→0. |
| 6 | Scope | **lake / REFERENCE_ONLY** (display + options-realism), NOT a brute-force engine input | Golden Rule #3 — the ENGINE stays NIFTY / BANKNIFTY / SENSEX; a Greek-as-a-vocabulary-condition would be a separate authorized ADR. |
| 7 | When to compute | **HYBRID** (read-time source-of-truth + optional pull-time materialization) — REFINED by §15 (`DEC-GREEKS-GENERATION-TIME-001`) to a COST-CLASS hybrid: compute-and-STORE the expensive IV+Greeks family at generation time, KEEP the cheap moneyness family read-time-derived | Flexible + fast; both use the SAME Rust core so results are bit-identical; §15 stores only the family whose per-read IV-solve cost is unbounded, keeping the cheap family staleness-proof. |

> **Note on #4:** the deep-research plan LOCKED calendar Act/365.25 for
> simplicity/reproducibility as its stated position; this defaults doc instead
> proposes NSE trading-minutes as the *more precise* default (business-time is
> the more accurate convention for intraday theta), and records calendar
> Act/365.25 as the explicit reproducibility alternative. This is an
> operator-decidable trade-off — recorded honestly, not silently chosen
> (Open Decision #5).

---

## 8. Honest precision envelope (no illusion — Golden Rule #10)

**EXACT.**
- European INDEX options (NIFTY / BANKNIFTY / SENSEX) via Black-76-on-the-forward
  closed form (spec §7.10 asserts European / cash-settled — see the §2.1
  UNVERIFIED caveat). Black-76-on-`F` is algebraically identical to BSM-with-`q`
  for European options (Hull 11e), so the naming is cosmetic given `q` is modelled.
- **Delta** is well-defined and IV-INDEPENDENT even in the deep-ITM /
  intrinsic-only regime, so it can be reported (~±1) where the vega-sensitive
  Greeks cannot.
- The IV round-trip is machine-precision via Jäckel, bounded iteration.
- BruteX ↔ TickVault bit-identity is genuinely free IF both link the same
  compiled Rust kernel (the trading-core contract) — modulo the cross-platform
  ULP caveat below.

**APPROXIMATE / KNOWN-BIAS (stamped on every output).**
- `q = 0` for indices over-prices calls / under-prices puts by ~0.004% daily
  (~0.11% monthly of premium at NIFTY's ~1.3% trailing yield). Defensible: fills
  use OBSERVED Groww bar prices (never a BSM-theoretical price), so P&L is
  market-anchored, and delta-bucket strike selection is a RELATIVE ranking the
  `q = 0` shift preserves. The bias direction is documented, not hidden.
- The ~212 single-stock F&O options are AMERICAN-style (early exercise + discrete
  dividends before expiry). Closed-form Greeks are NOT exact for them — either NA
  or explicitly flagged `american_style_approx` (Proposed Decision #1). There is
  NO exercise-style classifier in the code today (grep `european|american` = 0
  hits); style is derived from `instrument_type` (IDX vs EQ).
- IV is SOLVED from the traded option price, NOT read from the vendor (the Groww
  historical endpoint stores only OHLCV + OI). So the spec §7.7 "vendor IV
  authoritative + 50 bp cross-check" premise is STALE and is formally flipped to
  "IV solved from price". This reopens every deep-OTM / no-arb / tick-floor
  divergence the vendor cross-check was meant to sidestep — the edge-case matrix
  (§9) handles each by returning NA.
- Time-to-expiry: whichever convention is chosen (Proposed Decision #4:
  trading-minutes default vs calendar Act/365.25 alternative) is a modelling
  choice, stamped on output.
- Risk-free `r` is an EXTERNAL data dependency with a bias, not a blowup (rho
  sensitivity ≈ 1.3% weekly per 100 bp `r`-error).

**UNVERIFIED (flagged, cite-honest).**
- Groww's own IV / Greeks methodology (root-finder? mid vs LTP? `r` assumption?
  — NSE itself uses a fixed rate) is UNVERIFIED (spec §7.11) — moot for
  historical since we solve, relevant only if a live vendor-IV path is later
  adopted.
- The European-style claim for the 3 indices is spec-ASSERTED (§7.10), not
  independently re-verified — re-confirm against live NSE / BSE contract specs
  before coding.
- Cross-platform float determinism (the M4-Pro macOS reference per CLAUDE.md §8
  vs the Linux CI) for `exp / ln / sqrt / erf` is the #1 open numeric risk; the
  "bit-identical" golden is only truly frozen if a pinned Rust `libm` +
  deterministic `norm_cdf` is vendored, else the golden downgrades to a
  documented tolerance (stated honestly, not assumed).

---

## 9. Edge-case matrix (EVERY case → an honest output)

NA / UNKNOWN is the default honest output; a fabricated number is NEVER
surfaced. This mirrors the never-raises contract of `brutex/data/moneyness.py`.

| # | Condition | Detection | Output |
|---|---|---|---|
| 1 | FUT row (no strike / type) | `instrument_type == "FUT"` | all Greeks `—` (like moneyness NA); days_to_expiry still resolves |
| 2 | Missing / absent underlying spot | per-bar join returns None | all Greeks **NA** (no `S`) — never a fabricated spot |
| 3 | `T ≤ 0` (bar at / after the expiry instant) | expiry-instant − bar ≤ 0 | all Greeks **NA** |
| 4 | `T < 600 s` floor (gamma→∞, theta→−∞) | T below the hard floor | Greeks **NA / flagged** — never a divide-by-tiny-T blowup. The Rust kernel returns a clean `Err`, the Python layer maps to NA (a panic under `panic="abort"` would SIGABRT the process — so the kernel must NOT panic) |
| 5 | Price < intrinsic (no-arb violation) | `close < intrinsic` (time_value < 0) | IV **NA** + `no_arb_violation` flag; still show the (negative) time_value honestly |
| 6 | Intrinsic-only / time_value ≈ 0 (deep ITM at expiry) | vega below floor | **delta only** (well-defined, ~±1, IV-independent); IV / vega / gamma **NA / low-confidence** |
| 7 | Tick-floor saturated (price pinned at the min tick) | price ≤ 5× tick for ≥ 30 bars | IV **NA**, `tick_floor_saturated` flag; entry price must be > ₹0.25 (already coded, `executor.py:119-123`) |
| 8 | Deep OTM, vega→0, IV solve diverges | non-convergence / vega < 1e-6 | IV **NA**, admission-filtered; Jäckel (not Newton) minimizes this |
| 9 | Non-converged solve | solver returns None | IV + IV-derived Greeks **NA** |
| 10 | IV outside clamp [0.001, 5.0] | solver hits the bound | IV **NA / flagged** — never report the boundary as "the IV" |
| 11 | American stock option | underlying `instrument_type == "EQ"` | Greeks **NA or `american_style_approx`-flagged** (Proposed Decision #1) |
| 12 | Discrete dividend before expiry (stocks) | (stock branch) | folds into #11 — closed-form not exact |
| 13 | Put vs call sign | option_type | signed delta (CE +, PE −); gamma / vega same magnitude; theta / rho per convention |
| 14 | Weekend / holiday in `T` | per the chosen `T` convention | decay counted per the market convention; the NSE calendar resolves the expiry instant if expiry falls on a holiday |
| 15 | Zero-volume bar with a real close | volume == 0 | priceable but **low-confidence flag** |
| 16 | No bar at the minute (stale) | lake gap | Greeks **NA** — never a nearest-prior fabrication (`lake_source.py:126-129`) |
| 17 | Index vs stock misclassification | derive style from master `instrument_type`, NOT the symbol | UNKNOWN underlying → conservative NA / flagged |

Every emitted Greek row is STAMPED with: the `r`-source + version, the `q = 0`
bias direction, the exercise-style, and the flag set — so the number is always
traceable (Golden Rules #1 / #4 / #10).

---

## 10. The COMMON shared-crate architecture

**EXTEND `rust/trading-core/`, do NOT create a new crate or repopulate
`fno-math`.**

**Where (verified rationale).** `rust/trading-core/` is ALREADY the exact
bit-identical shared core the requirement names: a `cdylib` + `rlib`,
dependency-free pure-Rust core (TickVault links natively) with a PyO3 wheel for
BruteX behind an OPTIONAL `python` feature (`rust/trading-core/Cargo.toml:31`
`crate-type = ["cdylib", "rlib"]`; `:38-39` `default = []`,
`python = ["dep:pyo3"]`; `:49` `abi3-py312`), proven identical by the frozen
400-case golden (DEC-SHARED-CORE-001, MIGRATION.md #441). This is precisely the
DEC-SHARED-CORE-001 pattern. Add a `greeks` module to `trading-core` and
register its functions in the existing `_trading_core` pymodule. This inherits
FOR FREE: the wheel, the `pip install ./rust/trading-core` CI step, the
`brutex.math` facade auto-re-export (`brutex/math/__init__.py` `_reexport` is
programmatic over `_KERNEL_MODULES`), and the golden harness.

**Rejected alternatives.**
- A NEW crate (`rust/greeks-core/`) — duplicates `Cargo.toml` + `pyproject` /
  maturin + a third `pip install` CI step + its own golden harness + a
  `brutex.math` facade entry, all of which `trading-core` already has.
- `fno-math` — it is bit-kernels (popcount / blake3 / trade_sim) + compat, has
  NO bit-identity golden contract and NO native-TickVault sharing story; its
  empty `math/` stub is a scaffold, not a home. (Any stale Track-2 phase docs
  pointing Greeks at `fno-math/src/bsm.rs` are obsolete and are re-homed by this
  DEC-ID.)

**The dependency-free caveat.** `trading-core`'s core is deliberately
dependency-free so TickVault's native link stays clean. The Jäckel IV solver
needs either the external `jaeckel` crate or a vendored solver — keep it behind
a feature or vendor a small pure-Rust Rational + `erf` so the dependency-free
native-link contract is preserved (Open Decision #7). Strip the OI-based PCR /
buildup / aggregator from the recovered code entirely — BruteX cannot feed that
data.

**The bit-identity contract (BruteX + TickVault).** A NEW frozen
`tests/golden/fixtures/greeks_golden.json` (N cases:
`(S, K, T, r, q, type, price) → (iv, delta, gamma, theta, vega, rho)`), read by
BOTH a Rust `greeks_golden.rs` (bit-for-bit `assert_eq!` on `Vec<Option<f64>>`,
exactly like `ema_golden.rs:31-56`) AND the Python parity test
(`test_python_rust_parity.py` pattern). This IS the contract both repos satisfy
— the twin of `vocab_golden_vectors.json`. The float precedent already exists
(the EMA golden is bit-for-bit f64). The ONE caveat: transcendentals may force a
documented tolerance instead of bit-for-bit on some platforms (see §8) — state
honestly. Claim only the BruteX-SIDE gate + the frozen contract from this repo;
"bit-identical BruteX ↔ TickVault" cannot be PROVEN on the TickVault side without
the TickVault repo (out of scope, Golden Rule #10).

**Consumer delegation (COMMON, no math duplication).**
`brutex/options/greeks.py` = a thin marshalling wrapper: validate + pack into a
dataclass, delegate the arithmetic to `trading_core` (mirrors
`brutex/vocabulary/evaluator.py` and `brutex/indicators/pivots.py` delegation).
The auto-facade surfaces it as `brutex.math.greeks`. **REJECT a pure-Python
runtime fallback** — it double-implements the math (COMMON violation, Golden
Rule #11) and Python `math.exp` / `erf` vs Rust `libm` diverge in the last ULP,
breaking the very bit-identity the golden proves. `py_vollib` / QuantLib stay CI
oracles ONLY.

The PyO3 wrapper maps `Err → PyValueError` and validates inputs (`T ≥ 600 s`
clean-reject NOT panic under `panic="abort"`; `S` / `K` / `price` > 0; all-finite)
— mirroring the `compute_pivot_levels` `_impl` + wrapper pattern, the STATELESS
`compute_*` FFI form (not the stateful `VwapState` `#[pyclass]` form).

---

## 11. Phased PR roadmap

Every phase is gated. **Phase 0 (this document + the ADR) is docs-only and
requires operator go before Phase 1.**

### Phase 0 — Governance + spec reconcile (docs only, no code) — THIS PR
- **Deliverable:** this plan + the MIGRATION.md #1070 entry + the PROPOSED
  ADR-0015 (`DEC-GREEKS-SHARED-CORE-001`) that (a) explicitly reverses
  DEC-P0-025's Greeks deletion, scoped to the Track-2 options-realism leg + lake
  read-enrichment (ENGINE UNCHANGED); (b) reconciles TRACK2_OPTIONS_SPEC §7.1 /
  §7.7 from "vendor IV authoritative" to "IV SOLVED from price" (the lake stores
  no iv / spot); (c) re-homes Greeks from the stale `fno-math` phase-doc target
  into `trading-core`; (d) records the per-instrument model table (European
  index = exact Black-76; stock = American approx / NA).
- **Gate:** operator go recorded; ADR accepted; no Golden-Rule-3 silent-scope
  violation; the spec drift ledgered BEFORE any code.

### Phase 1 — Risk-free `r` data prerequisite (no Greeks yet)
- **Deliverable:** `brutex/options/risk_free.py` + either a fixed-`r` constant
  (Proposed Decision #2 default) or a `risk_free_daily.csv` loader with a
  money-market → continuously-compounded conversion
  `r_cc = (365/n)·ln(1 + y·n/365)` and a step-function lookup (no interpolation).
- **Gate:** the `risk_free` module at 100% line + branch; the `r`-source +
  version stamped; standalone, no Greeks dependency.

### Phase 2 — Shared Rust `greeks` kernel in `trading-core`
- **Deliverable:** `rust/trading-core/src/greeks/` (Black-76 call/put price +
  delta/gamma/theta/vega/rho + the Jäckel-or-vendored IV inverter; hard `T`-floor
  600 s clean `Err`; iteration cap; IV clamp [0.001, 5.0]; admission floors
  |delta| ≥ 0.02 / vega ≥ 1e-6). ONE pure fn `greeks(price, sigma, F, K, T, r,
  is_call)`. Recover `black_scholes.rs` / `historical_rates.rs` from
  `a87bce43^` as the STARTING POINT; strip PCR / buildup / aggregator; re-derive
  every constant against Groww / NSE. PyO3 `#[pyfunction]` in `python.rs`;
  re-export in `python/trading_core/__init__.py`; `brutex/options/greeks.py` thin
  wrapper (auto-facade → `brutex.math.greeks`). FREEZE `greeks_golden.json`.
- **Gate:** `greeks_golden.rs` bit-for-bit (or documented tolerance) reproduces
  the frozen key; Python ↔ Rust parity green; the cross-platform determinism
  decision landed (pinned `libm` / `norm_cdf` OR a tolerance); `cargo test` +
  `cargo-mutants` ≥ 85%; wrapper + kernel 100% line + branch; `py_vollib` 1e-10 +
  QuantLib oracles green; mypy strict + pyright + ruff clean.

### Phase 3a — `/db` F&O candle-table surface (the operator's primary ask)
- **Deliverable:** extend `_fno_derived_rows` (`db_view.py:2140`) to append
  `iv, delta, gamma, theta, vega, rho` off the EXISTING per-bar spot join (zero
  extra read); build `T` from the expiry-instant @ 15:30 IST; advertise the
  columns as `FnoCandleColumn(source="derived")` in `FNO_CANDLE_COLUMNS`. Full
  edge-case matrix (§9) → NA / flag, never a fabricated number. Every row stamped
  with `r`-source / q-bias / style / flags.
- **Gate:** `db_view` 100% line + branch; one edge-unit test per §9 row;
  O(page)-build / O(1)-bar preserved (no new I/O); read-only (ENGINE UNCHANGED);
  ruff / mypy / pyright clean.

### Phase 3b — `/movers` options surface
- **Deliverable:** add `delta` / `iv` cells to `OptionMover` +
  `compute_option_mover` (`movers_options.py`), off the same as-of snapshot join.
  No new read.
- **Gate:** `movers_options` 100% line + branch; corrupt-file-skip resilience
  preserved (DEC-MOVERS-CORRUPT-SKIP-001 #886); read-only accelerator; ENGINE
  UNCHANGED.
- **Status: SHIPPED (DEC-GREEKS-ENGINE-PR3B-001, MIGRATION.md #1133)** — post-#1111
  the cells are the STORED overlay (not a read-time re-derive): the shared
  `_read_future_session` returns the LAST kept bar's stored `iv`/`delta` off the
  SAME single frame read (`_last_bar_overlay`, schema-tolerant — legacy
  partition / stamped-§15.8-NULL bar / FUT ⇒ honest `None`, never backfilled);
  `OptionMover.iv/.delta` + `_option_row` project them full-precision; the
  `movers.html` options table shows IV + Delta (4dp display-only `num4`). Both
  movers modules re-verified 100% line+branch under their discovery stem sets.

### Phase 3c — Options executor delta-target activation (backtest realism)
- **Deliverable:** a lake-backed `DeltaChainProvider` feeding
  `resolve_delta_target_strike` → activate the dormant strike rules 8 / 9 / 10;
  stamp `iv_at_entry` from the solve. Enforce the admission floors + the
  tick-floor entry > ₹0.25 (already coded).
- **Gate:** `executor` + `strike_rules` 100% line + branch; rules 8 / 9 / 10
  unit + integration; the signed-delta convention pinned; still NOT the
  brute-force engine (options REFERENCE_ONLY); property tests P-O6..P-O10; bench
  O(1)/bar + a pinned IV-iteration cap, p99 ratio ≤ 1.1.

### Phase 4 — TickVault contract (repo boundary; contract only)
- **Deliverable:** the frozen `greeks_golden.json` + the shared `trading-core`
  kernel ARE the BruteX-side bit-identity contract; the live path feeds
  `sigma = Some` (vendor / live IV). Document that TickVault adoption (linking the
  crate natively + reproducing the golden) is out-of-repo and cannot be proven
  from BruteX.
- **Gate:** the BruteX-side golden gate green; claim ONLY the frozen contract +
  the BruteX side (Golden Rule #10 — no cross-repo bit-identity claim without
  TickVault repo access).

---

## 12. O(1) analysis + the test plan

**O(1) per bar.** The per-bar Greek is a closed form = a fixed count of
`exp / ln / sqrt / erf` + arithmetic → O(1) per bar, O(1) space (NOT an
O(1)-total claim). The spot join is O(page) build + O(1)/bar lookup (already the
case for moneyness, `db_view.py:2204`). The IV solve is the ONLY unbounded risk
— Jäckel bounds it to ~2 Householder iterations (machine precision) → O(1); a
bench MUST PIN the iteration cap so a regression fails CI (Golden Rule #5, p99
ratio ≤ 1.1).

**Test plan (100% + golden + property + bit-identity + bench).**
- **GOLDEN (bit-identity contract):** a NEW frozen
  `tests/golden/fixtures/greeks_golden.json` (N ATM / ITM / OTM cases), read by
  BOTH a Rust `greeks_golden.rs` and the Python parity test — the direct twin of
  `ema_golden.rs`. Decide bit-for-bit `assert_eq!` vs a documented tolerance
  (interacts with §8 cross-platform determinism, Open Decision #6).
- **ORACLE (CI only, never the runtime path):** Black-76 vs
  `py_vollib.black_scholes_merton` to 1e-10 over a 10K synthetic grid +
  QuantLib `AnalyticEuropeanEngine` as a second independent oracle.
- **PROPERTY (hypothesis + proptest):** put-call parity; delta ∈ [0,1] CE /
  [−1,0] PE; gamma ≥ 0; vega ≥ 0; theta ≤ 0 long premium; IV round-trip
  `price → iv → price` within tol; `T < 600 s` → clean `Err`; IV-solve iteration
  cap bounded.
- **EDGE UNITS:** one test per row of §9 (tick-floor → UNKNOWN; |delta| < 0.02 /
  vega < 1e-6 excluded; missing spot → None; price < intrinsic → no_arb flag;
  American stock → flagged / NA; expiry-day `T`-clamp).
- **BENCH:** O(1)/bar Greek with `p99(1M) / p99(1K) ≤ 1.1`; a PINNED IV-solve
  iteration-cap bench.
- **MUTATION:** mutmut (Python wrapper) + cargo-mutants (Rust kernel) ≥ 85% —
  float BSM math is mutation-sensitive (sign flips in d1 / d2, off-by-one), so
  budget it explicitly.
- **CI:** a dedicated `--cov=brutex.options.greeks --cov=brutex.options.risk_free
  --cov-branch --cov-fail-under=100` job cloned from the existing per-module
  pattern + the `cargo test` / `cargo-mutants` job for `rust/trading-core`; the
  kernel rides the existing `pip install ./rust/trading-core` (no new crate
  install).

---

## 13. Open decisions (operator must resolve before / during Phase 1-2)

These are the questions the operator resolves; §7 records the RECOMMENDED
defaults for each, all overridable.

1. **MODEL PER INSTRUMENT** — European index = exact Black-76. Do the ~212
   REFERENCE_ONLY single-stock (AMERICAN) options get (a) NO Greeks (NA) or (b) a
   European-approx flagged `american_style_approx`? No exercise-style classifier
   exists today; the conservative default is NA — §7 recommends (b) so stocks are
   still usable. Operator picks a / b + whether stock Greeks are even in scope.
2. **EXERCISE-STYLE / SCOPE** — confirm Greeks serve the Track-2 options-realism
   leg + lake read-enrichment ONLY, and NEVER feed the 52→60-condition
   brute-force vocabulary unless separately authorized (options stay
   REFERENCE_ONLY, Golden Rule #3). Explicit so the crate addition is not read as
   a silent engine-scope expansion.
3. **RISK-FREE `r` SOURCE** — fixed `r` (reproducibility, matches NSE's own
   fixed-rate chain) vs a versioned T-bill daily CSV (accuracy). rho sensitivity
   ≈ 1.3% / 100 bp — a bias, not a blowup. §7 recommends fixed (~6.5%) to start.
4. **DIVIDEND YIELD `q`** — keep `q = 0` LOCKED for indices with the documented
   bias stamped on output (§7 recommends this) vs adopt a published daily yield
   as `q_at_pricing`.
5. **TIME-TO-EXPIRY `T`** — NSE trading-minutes / business-time (more precise
   for intraday theta; §7 recommends this default; `session_minutes_per_day()`
   exists) vs calendar Act/365.25 (simpler, more reproducible).
6. **CROSS-PLATFORM FLOAT DETERMINISM + GOLDEN TOLERANCE** — vendor a pinned
   Rust `libm` + deterministic `norm_cdf` so the golden stays bit-for-bit (like
   `ema_golden`), OR downgrade the greeks golden to a documented ~1e-12 tolerance
   (weakens "bit-identical", stated honestly). The #1 numeric risk.
7. **IV SOLVER SOURCING** — re-add the external `jaeckel` crate (machine
   precision, but adds a dependency to the dependency-free native-link contract),
   OR vendor a small pure-Rust Rational + bisection + `erf` fallback guarded by
   the admission floors. Affects TickVault native-link cleanliness.
8. **STORAGE** — read-time-derived (never stored, mirrors moneyness; §6 + §7
   recommend HYBRID: derive-first, optional-cache-later) vs a lake-schema change
   adding delta/gamma/theta/vega columns (a bigger, REFERENCE_ONLY-scoped change).
   **REFINED by §15 (`DEC-GREEKS-GENERATION-TIME-001`):** the refinement RESOLVES
   this toward COMPUTE-AND-STORE for the expensive IV+Greeks family (the
   `FNO_CANDLE_LAKE_SCHEMA_VERSION` 2→3 additive column bump + the
   `greeks_enrich_after_pull` hook) while keeping the cheap moneyness family
   read-time-derived — CONDITIONAL on the shared kernel + frozen golden landing
   and on this decision's coupled §13 items #3/#4/#5 (`r`/`q`/`T`) being LOCKED
   FIRST (storing freezes them into the bytes; a later change is a clean O(bars)
   bulk re-enrich, not a correctness hazard). Operator-overridable.
9. **DO GREEKS EVER FEED THE ENGINE** — for now REFERENCE_ONLY read-enrichment +
   the delta-target strike rules (execution realism), NEVER an engine input. A
   Greek as a 61st vocabulary condition would be a separate authorized ADR (like
   the NIFTY-futures engine-scope amendment ADR-0012), NOT part of this work.

---

## 14. Standard external sources cited

- **Black-76 / BSM closed forms + Greeks + European ≡ Black-76-on-`F`:** J.
  Hull, *Options, Futures, and Other Derivatives*, 11th ed. (ch. 15 BSM, ch. 17
  Greeks, ch. 18/19 Black-76 / forward pricing). Standard textbook reference.
- **IV inversion (Jäckel "Let's Be Rational"):** P. Jäckel, *"Let's Be
  Rational"*, 2015 — the bounded, machine-precision Householder inversion in
  forward/Black space.
- **CI oracles (never runtime):** `py_vollib` (`black_scholes_merton`) and
  QuantLib (`AnalyticEuropeanEngine`).
- **Codebase facts:** every `file:line` above traces to the repository at HEAD
  and was re-verified in the deep-research session (Golden Rule #1).
- **NSE / BSE index-option exercise-style + settlement:** asserted by
  `docs/track2/TRACK2_OPTIONS_SPEC.md` §7.10 (citing NSE Clearing / BSE contract
  pages), flagged UNVERIFIED here pending an independent live re-confirm (§2.1).

---

## 15. Refinement — GENERATION-TIME compute-and-store (`DEC-GREEKS-GENERATION-TIME-001`)

> **Working decision ID:** `DEC-GREEKS-GENERATION-TIME-001` — a REFINEMENT of
> `DEC-GREEKS-SHARED-CORE-001` / ADR-0015, both PROPOSED, docs-only, fully
> reversible, OPERATOR-OVERRIDABLE. This section ADDS a generation-time
> storage stance for the expensive IV+Greeks family; it does NOT delete any of
> the seven §7 / ADR-0015 decisions — it sharpens decision #7 (When to compute) +
> §13 open decision #8 (Storage) from the earlier "read-time source-of-truth +
> optional cache" HYBRID to a HYBRID drawn on COST-CLASS. Sourced from the 14-agent
> verification panel LOCKED design.
>
> **Status honesty up front (Golden Rule #1 / #10 — refreshed by
> DEC-GREEKS-ENGINE-PR2B-001).** The gated build phases have now LANDED this
> section's core: Phase 1 shipped the stamped `RiskFreeSource` seam
> (`brutex/options/risk_free.py`, DEC-GREEKS-ENGINE-PR1-001 #1094); Phase 2
> shipped the shared Rust Black-76 kernel + verified Jäckel IV inversion +
> the frozen `tests/golden/fixtures/greeks_golden.json`
> (`rust/trading-core/src/greeks/` + `brutex/options/greeks.py`,
> DEC-GREEKS-ENGINE-PR2-001 #1100); Phase 2b shipped THIS section's
> generation-time store — the `FNO_CANDLE_LAKE_SCHEMA_VERSION` 2→3 additive
> overlay columns, the `greeks_enrich_after_pull` hook
> (`brutex/data/greeks_enrich.py`, wired at the `historical_pull.py` post-drain
> seam), and the locked `(r, q=0, T=Act/365-to-15:30-IST)` provenance stamp
> (DEC-GREEKS-ENGINE-PR2B-001). Still true: the engine consumes ZERO Greeks
> (all 60 vocabulary bits are price / volume-derived); the Phase-3 read
> surfaces (`/db` stored-column read, `/movers` cells, executor delta-target,
> §15.10 derived-TF close-snapshot propagation) are NOT yet built.

### 15.1 Verdict — a HYBRID drawn on cost-class, not "store everything"

**Compute-and-STORE at generation time the EXPENSIVE family — `iv`, `delta`,
`gamma`, `theta`, `vega`, `rho` plus the model-input provenance stamp — and KEEP
the CHEAP family — `moneyness`, `spot`, `intrinsic`, `time_value`,
`days_to_expiry` — read-time derived (UNCHANGED, per §4).** The lever is a genuine
cost asymmetry the earlier §4 / §6 stance did not weigh: moneyness is trivial
O(1) arithmetic (a subtract, a divide, a round) so deriving it on every read is
free and staleness-proof, whereas each Greek carries a per-bar **IV root-solve**
(Jäckel) whose iteration count dominates — paying that solve on every `/db` page
open and every `/movers` rank, forever, for IMMUTABLE past bars is the waste that
store-once eliminates. This is a **surgical exception carved for the one family
whose per-read cost is unbounded**, NOT a reversal of the "moneyness is never
stored" philosophy — which stays correct and preserved for the cheap columns.

**This verdict is CONDITIONAL and gated** (§15.6): it cannot ship until (a) the
shared `trading-core` Greeks kernel lands with a green frozen `greeks_golden.json`
(bit-for-bit or a documented tolerance), and (b) the risk-free-rate source,
dividend `q`, and time-to-expiry convention (§13 open decisions #3 / #4 / #5) are
LOCKED — because storing freezes those choices into the bytes. Because those
inputs can be locked before the first write and the enrichment pass is idempotent
`latest-wins`, a later convention change is a clean bulk re-materialize (a
one-shot O(bars) re-run), NOT a correctness hazard.

### 15.2 Backtest ↔ live parity — one shared kernel, two call sites

The parity argument is the same one that already makes the 52→60-condition
vocabulary bit-identical across BruteX (PyO3 wheel) and TickVault (native): a
single compiled kernel in `rust/trading-core/`, pinned to a frozen answer-key on
every build (`DEC-SHARED-CORE-001`, MIGRATION.md #441,
`tests/golden/fixtures/vocab_golden_vectors.json`).

- **The one function** is the pure, stateless, per-bar-pointwise kernel of §2.3 /
  ADR-0015 D3 — `greeks(price: Option<f64>, sigma: Option<f64>, forward, strike,
  t_years, rate, is_call) -> Result<GreeksOut, String>` — with NO wall-clock, NO
  RNG, NO ordering or cross-bar dependence. Every output is a function of that
  same bar N's own data (the option's own `close` / `strike`, the underlying spot
  at the IDENTICAL timestamp instant, the frozen `(r, q, T)` inputs). No
  rolling / forward window, no nearest-next fallback: a missing spot minute is an
  honest `None`, never bar N+1. **Look-ahead-clean by construction** — structurally
  the same guarantee `_fno_derived_rows` already satisfies for moneyness
  (`db_view.py` exact `_utc_instant` join).
- **Call site A — post-pull enrichment (historical / backtest path):**
  `sigma=None` → the kernel solves IV from the option premium first, then computes
  the five Greeks (§15.4).
- **Call site B — live tick-aggregator (future live path):**
  `sigma=Some(vendor_or_tickvault_iv)` → the kernel skips the solve and computes
  Greeks directly from the supplied sigma. Both branches converge on the SAME
  closed-form kernel, so a live-stamped Greek and a historically-enriched Greek for
  the same `(spot, strike, T, r, q, is_call)` are identical. *(Honest caveat: the
  live feed was removed in full — `DEC-LIVE-REMOVE-001` #862 — so call site B is a
  design seam, not a live system today. Parity for it is a property of the kernel,
  provable now; the integrated live producer is future work.)*
- **Bit-identity via the frozen answer-key:** a NEW
  `tests/golden/fixtures/greeks_golden.json`
  (`(S, K, T, r, q, type, price) → (iv, delta, gamma, theta, vega, rho)`) read by
  BOTH a Rust `greeks_golden.rs` (`assert_eq!`) AND a Python parity test — the
  exact twin of `vocab_golden_vectors.json`. **HONEST limit (Golden Rule #10):**
  transcendentals (`exp / ln / sqrt / erf`) in the IV solve may force a
  DOCUMENTED TOLERANCE instead of literal bit-for-bit on some platforms (§13 open
  decision #6, the #1 numeric risk); TickVault-side bit-identity is unprovable from
  this repo. So parity is "one kernel, one frozen key, documented-tolerance-if-needed,"
  not an un-caveated bit-for-bit claim.

### 15.3 The exact column restructure (additive, backward-compatible)

The physical F&O schema today is 7 columns (`timestamp, open, high, low, close,
volume, open_interest`) rendered by `parquet_writer._render_bytes`. The
restructure is **additive and backward-compatible**: every new column is
nullable and appended after `open_interest`, so a legacy 7-column partition reads
them as NULL with NO migration.

| Column | dtype | nullable | meaning |
|---|---|---|---|
| `iv` | Float64 | yes | Solved implied volatility (annualized). **Full precision — NOT 2dp-snapped** (`DEC-MONEY-2DP-001` forbids rounding statistical values). |
| `delta` | Float64 | yes | ∂price/∂spot. Full precision. |
| `gamma` | Float64 | yes | ∂delta/∂spot. Full precision. |
| `theta` | Float64 | yes | ∂price/∂time. Full precision. |
| `vega` | Float64 | yes | ∂price/∂σ. Full precision. |
| `rho` | Float64 | yes | ∂price/∂r. Full precision (kept because `r` is first-class). |
| `spot_at_bar` | Float64 | yes | Underlying spot close at the SAME minute (₹). The `_underlying_spot_close_by_ts` join result MATERIALIZED — and the ONE new column that IS 2dp-snapped as a price. |
| `t_years_used` | Float64 | yes | Time-to-expiry in years, under the frozen `T` convention. |
| `rate_used` | Float64 | yes | Risk-free `r` frozen into this bar's Greeks. |
| `greeks_provenance_id` | Int32 | yes | Packs `(GREEKS_FORMULA_VERSION, r_source_id, q_bias_id, tte_convention_id, style_flag)` — every input frozen into the bytes. The staleness-detection key (§15.6 top risk). Dividend `q` folds into this stamp. |

**Why the stamp columns are MANDATORY, not optional.** A bare Greek column with
no provenance re-introduces exactly the "frozen `r/q/T` into stored bytes"
staleness §6 warns against. With `t_years_used` + `rate_used` +
`greeks_provenance_id` stored alongside, every stored Greek is self-describing,
auditable, and re-derivable — a `greeks_provenance_id` that differs from the
current constant is a POSITIVE re-enrich signal, turning silent drift into a clean
generation-stamped invalidation (the same `master_revision` / write-generation
discipline used across the repo).

**`FNO_CANDLE_COLUMNS` change (`fno_candle_lake.py`).**
- **Flip** `iv, delta, gamma, theta, vega, rho, spot` from `source="derived"` →
  `source="bar"` (now physical), and **add** `source="bar"` rows for
  `t_years_used`, `rate_used`, `greeks_provenance_id`.
- **Keep** `moneyness`, `intrinsic`, `time_value`, `days_to_expiry` as
  `source="derived"` — pure, staleness-proof, no solve. (`spot` moves to stored
  because the enrich pass already materializes it for the solve;
  `intrinsic` / `time_value` stay derived — trivial arithmetic off `close` + the
  now-stored `spot`.)
- **Bump `FNO_CANDLE_LAKE_SCHEMA_VERSION` 2 → 3** (adding physical columns is a
  shape change) and re-register `schema_meta` (`fno_candle_lake.columns`
  auto-updates via the `c.name` comprehension). The on-disk change is nevertheless
  additive because every column is nullable and appended.

**`/db` view change (`db_view.query_candles`, `_fno_derived_rows`).**
- Read `iv/delta/gamma/theta/vega/rho/spot_at_bar` as REAL selectable Parquet
  columns (a plain projection — NO per-bar solve in the hot read path).
- **Keep a derived fallback:** when a bar's stored `iv IS NULL` (a legacy
  partition, an unsolvable bar, or a future leg), fall back to the existing
  read-time §4 path — `_underlying_spot_close_by_ts` for spot + honest
  `"?"` / `None` for the Greeks. The existing `_fno_derived_rows` becomes the
  NULL-branch.
- **Advertise per-cell provenance:** a non-NULL Greek reads as `stored`; a NULL
  Greek reads as `derived` / `absent`; a bar whose `greeks_provenance_id` ≠ the
  current constant reads as `stale` (never served as authoritative — §15.6 top
  risk mitigation).

**Writer-path changes (the only hard part; the sink / flock / atomic spine is
reused verbatim).**
- `_render_bytes`: extend the rows dict + schema map with the new columns; branch
  on bar type (`getattr(bar, "iv", None)`) so a plain spot `CandleBar` still
  renders 7 physical columns and only an F&O Greeks bar carries the extras. Greeks
  bypass `_snap_price_expr` / `_PRICE_COLUMNS` (OHLC-only); only `spot_at_bar`
  rides the 2dp price snap.
- `_read_existing_bars` + `_reconstruct_bars_per_row`: **schema-tolerant
  read-back** — for each new column use `get_column(name)` if present else a
  `None`-filled series, so a LEGACY 7-column partition reads them as NULL instead
  of `KeyError`-ing on `df["iv"].to_list()`. *This is the single biggest
  correctness trap in the whole change* — the merge path reads every existing
  partition on re-pull.
- `_bar_values_equal`: **stays OHLCV+OI-only** — a Greek difference at the same
  timestamp must NOT trip keep-first (Greeks are a re-derivable overlay, not
  source truth). Greek corrections are handled by the enrich sink's `latest-wins`
  mode, not by keep-first conflict.

**Bar-model note.** Introduce a dedicated `FnoGreeksBar` (frozen slotted
dataclass wrapping the 7 OHLCV fields + the Optional Greek / stamp fields) rather
than widening `CandleBar` — it avoids every spot writer carrying always-`None`
Greek fields. The 7 OHLCV fields reuse `_validate_price_bar`; the Greek fields
skip price validation.

### 15.4 The enrichment phase — `greeks_enrich_after_pull`

**A new `greeks_enrich_after_pull` hook — the exact F&O twin of
`_resample_daily_after_pull` (`pull_controller.py`).**

- **Where it runs.** In the F&O ingest tail, AFTER the F&O minute phase completes
  but BEFORE the `finally`'s `end_pull_ledger`, guarded `if not dry_run:` and
  `if fetch_pipeline is not None` (after the pipeline drain folds the real durable
  `bars_appended` back into `merged_pairs`). The whole body is wrapped in one
  `try/except Exception → log "fno.greeks_enrich.error" → return`, so it can NEVER
  fail the pull (best-effort, per-contract-quarantined exactly like
  `derive_daily_for_targets`; one bad contract never sinks the pass, with a
  `no_spot` tally mirroring the `no_minute` count in `spot_daily_resample.py`).
- **The spot precondition is guaranteed by construction in the full pipeline.**
  `run_full_pipeline` is hard-ordered SPOT → METADATA → HISTORICAL
  (`pipeline_pull.py`); `pull_all_spot` has fully returned and written durable
  1-minute spot bars before F&O historical even STARTS. The spot window
  `[2020-01-01, prev-day]` fully contains any option's ≤30-day lookback, so EVERY
  option-bar minute has an underlying spot — the same guarantee `db_view`'s
  moneyness join already relies on. In the standalone `/pull-historical` path (no
  spot pull in the same run) the pass relies on spot already in the lake from a
  prior pull; when spot is absent it is an honest no-op (Greek NULL), never a
  blocker.
- **The spot-join.** Lift `db_view._underlying_spot_close_by_ts` to a shared
  `brutex/data/` helper (COMMON, per Golden Rule #11) — keyed on
  `(exchange, underlying, interval, ts)`, returning `{utc_instant → Decimal(close)}`,
  window-bounded via `ist_span_partition_files` (O(pulled-window), NOT
  O(lake-depth)). Per bar: decode `(underlying, expiry, strike, option_type)` via
  `parse_fno_symbol`, `spot = spot_by_ts.get(bar_instant)` (EXACT `_utc_instant`
  join, NO nearest-fallback), `T = (expiry − bar_ist)/convention`,
  `forward = spot·e^((r−q)T)`, then `greeks(price=close, sigma=None, forward,
  strike, t_years=T, rate=r, is_call)` — solve IV then the five Greeks off the one
  shared kernel.
- **The flock-guarded write.** Write through a `ParquetBarSink` with
  **`merge_mode="latest-wins"`** (the exact mode the derived 1-day self-heal uses,
  `spot_daily_resample.py`). The enrich write goes through the SAME
  partition-flock read-modify-write spine (`parquet_writer.py` + `partition_lock.py`,
  `DEC-LAKE-PARTITION-FLOCK-001` #887): the flock is on a `sha256(dest)` sidecar
  per partition, so a concurrent live pull on the same partition serializes with
  the enrich instead of lost-updating, with no new lock class and no cross-partition
  deadlock (same lock order everywhere).
- **Incremental (only new / stale windows).** The reader is window-bounded to the
  pulled span, so a normal pull re-enriches only the bars it just touched. A bar
  whose stored `greeks_provenance_id` already equals the current constant is
  skipped (O(1) stamp compare, no recompute). A bulk `brutex reenrich-greeks --all`
  forces the whole lake when the formula version bumps.
- **Re-pull idempotence.** F&O windows CAN defer-and-re-pull (`option_ingest.py`
  transient defer; empty-window re-pull) and a later complete-session pull can
  CORRECT the bars. Because the enrich sink is `latest-wins` (not keep-first) and
  the compute is a pure function of the corrected bar + spot, a re-pulled /
  corrected minute re-solves and OVERWRITES the stale Greek — a Greek written
  against a partial-session or empty window is corrected, never permanently frozen.
  An identical re-enrich restates identical bytes → the atomic-write hash matches →
  no rewrite. This satisfies Golden Rule #7 (same inputs → same outputs,
  bit-for-bit).

### 15.5 Model + honest scope

- **Model.** Black-76 on the forward `F = S·e^((r−q)T)` (EXACT, algebraically
  identical to BSM-with-`q` for European options, Hull 11e) for European
  index / futures options; American single-stock → approximation or NA
  (single-stock F&O is REFERENCE_ONLY, §2.1). Exercise style is derived from the
  underlying's master `instrument_type` (IDX → European exact, EQ → American),
  UNKNOWN → conservative NA.
- **Risk-free rate is an ASSUMPTION** → stored + labelled via the provenance stamp
  (`rate_used` + `greeks_provenance_id`). The in-tree constants already contradict
  each other (`RISK_FREE_RATE=0.068` in `rust/fno-math/src/compat/constants.rs`
  vs `default_risk_free_rate()=0.10` in `config.rs`) and MUST be reconciled into
  one labelled source as part of locking §13 decision #3 (§15.6).
- **IV solver = Jäckel "Let's Be Rational"** (Peter Jäckel, *"Let's Be Rational"*,
  2015 — bounded, machine-precision, effectively O(1)). Non-convergence (deep-OTM
  zero price, price below intrinsic, past expiry) → NULL, never a divergent Newton
  value.

### 15.6 Gating (the verdict is CONDITIONAL)

> **Gate status (DEC-GREEKS-ENGINE-PR2B-001):** BOTH gates are now SATISFIED —
> (a) the shared kernel + frozen `greeks_golden.json` merged GREEN
> (DEC-GREEKS-ENGINE-PR2-001 #1100); (b) `r` is locked through the PR-1
> `RiskFreeSource` seam (fixed 6.5% default / `BRUTEX_RISK_FREE_CSV` T-bill
> curve, DEC-GREEKS-ENGINE-PR1-001 #1094), `q = 0` per ADR-0015 §3 #3, and the
> `T` convention is locked as calendar Act/365 seconds to 15:30 IST on expiry
> (the kernel's own day-count; the §3 #4 trading-minutes refinement stays an
> operator-overridable stamp-driven follow-up). All three are packed into the
> stored `greeks_provenance_id`, so a later revision is a clean stamp-driven
> re-enrich, exactly as designed below.

The store-at-generation write CANNOT ship until BOTH:

- **(a)** the shared `trading-core` Greeks kernel lands with a GREEN frozen
  `greeks_golden.json` (bit-for-bit or a documented tolerance, §13 open decision
  #6), AND
- **(b)** the risk-free-rate SOURCE, the dividend `q`, and the time-to-expiry
  CONVENTION (§13 open decisions #3 / #4 / #5) are LOCKED — because storing freezes
  them into the bytes.

Sequencing: the schema columns MAY ship first (ready-and-NULL, honest + additive),
but no Greek byte is written until (a) + (b). Ship the kernel + golden as their own
PR ahead of the enrich hook. Because the inputs can be locked before the first
write and the enrich pass is idempotent `latest-wins`, a later convention change is
a CLEAN O(bars) bulk re-enrich, NOT a correctness hazard.

**Top risks (with concrete mitigations).**
1. **The IV-solve model inputs do not exist in the write path today — a BLOCKER.**
   The vendor feed returns only `[ts,o,h,l,c,vol,oi]`; the lake stores NO IV, NO
   risk-free rate, NO per-option spot. Greeks require SOLVING IV + a joined spot +
   an `(r,q,T)` assumption, and the Black-76 kernel + Jäckel solver are NEW
   `trading-core` code not yet built. **Mitigation:** the gate above — schema
   columns may ship ready-and-NULL, no Greek byte until the kernel + golden land
   and #3/#4/#5 are locked.
2. **Silent formula-version / (r,q,T) staleness across years of lake — the risk
   uniquely CREATED by storing.** After any bump, every already-written partition
   would serve a stale-but-plausible Greek that looks authoritative — a Golden-Rule
   #1 (no-stale-illusion) violation at the scale of the whole lake. **Mitigation:**
   the `greeks_provenance_id` stamp + `latest-wins` + a detect-heal loop (re-enrich
   any bar whose stamp ≠ current) + a read-path guard that renders mismatched bars
   `stale` (or falls back to read-time recompute). **Residual, stated honestly:**
   there is a genuine staleness WINDOW between a version bump and the completion of
   a bulk re-enrich over years × ~212 underlyings — a multi-hour, operator-visible,
   resumable lake rewrite. It is NOT free; the read guard must render un-healed bars
   `stale` throughout the migration.
3. **Cross-platform IV-solve non-determinism.** The Jäckel transcendental path
   (`exp/ln/sqrt/erf`) may not be bit-reproducible across compilers / platforms.
   **Mitigation:** pin the kernel to `greeks_golden.json` in CI on the Linux runner,
   stamp `greeks_provenance_id` into every tuple, and GATE the store-at-generation
   write on a green bit-for-bit (or documented-tolerance) golden run before any
   Greek byte is frozen.

### 15.7 O(1) / complexity — honest, not "literal O(1)-total"

- **Per-bar compute:** O(1) arithmetic for `delta/gamma/theta/vega/rho` once `iv`
  is known, but the **IV solve is an iterative root-find — O(iterations)/bar, the
  ONE genuinely unbounded per-bar cost.** Bounded in practice (Jäckel, fixed
  iteration cap) but the cap MUST be a pinned CI bench gate (Golden Rule #5, §12) —
  NOT an un-caveated O(1) claim.
- **Enrichment pass:** O(bars-in-pulled-window), run ONCE per pull,
  window-month-bounded via `ist_span_partition_files` (NOT O(lake-depth)). No new
  I/O beyond the one spot read + the partition read / write the pass already needs.
- **Read:** O(1) per bar — a plain Parquet column projection, no join, no solve —
  strictly FASTER than today's read-time derive (which re-pays the O(page)
  spot-join + per-bar solve on every open). This read-latency collapse is the
  entire case for storing.
- **Backfill / formula re-materialize:** honestly O(all-stored-F&O-bars), a
  one-shot bulk re-write — explicitly NOT claimed cheap or O(1). The provenance
  stamp makes it incremental / resumable (only stale partitions rewrite).
- **NOT a literal O(1)-total.** The first enrich of each window pays the real
  bounded solve; a re-pull re-enriches at the same cost (idempotent via the stamp).
  That is exactly the store-at-generation trade.
- **Storage delta (honest):** ~9 Float64 + 1 Int32 ≈ 72 bytes/bar raw, compressing
  hard under Parquet dict / RLE on smooth Greek series + NULL-heavy FUT / deep-OTM
  columns → realistically ~30–50 bytes/bar on disk; over the F&O option universe a
  rough single-digit-to-low-tens of GB added — ACCEPTABLE on the 8 TB external SSD
  envelope (§8), scaling linearly (a fixed multiple of the existing bar count),
  never super-linearly.

### 15.8 Edge cases — NULL is never fabricated

Every unsolvable / unknown input stores an HONEST NULL with a stamped reason,
never a made-up value (mirroring the never-raises `moneyness.py` and the §9 read
matrix, now written to STORED columns).

| Case | Handling |
|---|---|
| Legacy 7-column partition on disk | schema-tolerant read-back fills the new columns NULL (not `KeyError`); the merge round-trip re-renders byte-identically; `/db` shows NULL → derived fallback. **The single biggest correctness trap** (§15.3). |
| No underlying spot at a bar's minute (1m gap) | spot join returns no key → `spot_at_bar=NULL`, `iv=NULL`, all Greeks NULL, stamp `no_spot`. Never a fabricated / carried-forward next-bar spot. |
| FUT leg / undecodable symbol | `style_flag=NA`, all six Greeks + inputs NULL; `days_to_expiry` (cheap derived) still resolves. Never a bogus Greek for a future. |
| American single-stock option (~212 REFERENCE_ONLY EQ names) | closed-form Black-76 is NOT exact → stamp `american_style_approx` (default) or NULL per operator choice; NEVER silently presented as exact (Golden Rule #10). |
| IV solve non-convergence / vega→0 (deep-OTM/ITM near expiry) | Jäckel returns `Err` → `iv=NULL`, Greeks NULL, stamp `iv_solve_failed`; never a divergent value / NaN / inf; solve error quarantined per-contract, the pull continues. |
| Bar exactly AT expiry (T ≤ 0) | `iv/Greeks=NULL` + `at_expiry` stamp (theta/gamma blow up as T→0), distinguishable from a solver failure via the stamp. |
| Bar strictly AFTER expiry (future timestamp) | a data-integrity error already dropped upstream by `drop_bars_after` (`DEC-FETCH-PARSER-HARDENING-001`) — should never reach enrich. |
| Negative time-value (close < intrinsic; below parity) | do NOT feed a negative time-value into the solver → `iv=NULL` + Greeks NULL + `negative_time_value` stamp. Never a spurious root. |
| Re-pull re-fetches the SAME minute | OHLCV keep-first no-ops (`bars_net_new=0`); enrich re-runs against identical inputs → `latest-wins` restates an IDENTICAL tuple → byte-identical render → effective no-op (Golden Rule #7). |
| Partial-session window filled by a LATER complete pull | the option bar's `spot_at_bar` changes → `latest-wins` re-enrich CORRECTS the previously-stale Greek. |
| Standalone `/pull-historical`, no spot in the same run | relies on spot already in the lake; spot absent → honest no-op (Greek NULL), tallied like `no_minute`, never a crash; fills on the next pull once spot exists. |
| Rate / model / convention revision | stored `greeks_provenance_id` ≠ current → detectable re-enrich (clean, not silent); the read guard renders un-healed bars `stale`; the stamped inputs turn a formula change into a positive invalidation signal. **Scope note (DEC-GREEKS-CORRECTNESS-AUDIT-001):** a same-KIND rate-VALUE revision (a revised `BRUTEX_RISK_FREE_CSV` / re-configured fixed rate) carries no stamp bit — it is detected by the reuse gate's `rate_used` VALUE-check, not the stamp. |

### 15.9 Two call sites to document (recorded in §5 + ADR-0015 D8)

- **(A)** `greeks_enrich_after_pull` — the F&O twin of `_resample_daily_after_pull`,
  the `sigma=None` solve path (§15.4).
- **(B)** the live tick-aggregator inline stamp — the `sigma=Some` path, FUTURE
  (the live feed is currently removed, `DEC-LIVE-REMOVE-001` #862).

Both converge on the one shared `greeks(...)` kernel, pinned by the NEW
`tests/golden/fixtures/greeks_golden.json`. Per Golden Rule #10 nothing here is
shipped: this refinement is the LOCKED TARGET for the gated build PRs, PROPOSED,
docs-only, operator-overridable.

### 15.10 Derived-timeframe propagation — CLOSE-SNAPSHOT, never aggregated (LOCKED semantics, `DEC-GREEKS-ENGINE-PR1-001`)

> Operator-directed semantics lock (2026-07-02, recorded in the Phase-1 PR
> `DEC-GREEKS-ENGINE-PR1-001`, MIGRATION.md #1094). *(Status:
> DEC-GREEKS-ENGINE-PR3-001 IMPLEMENTED the rule at the ONE canonical seam —
> `tf_resampler.resample_1m_to_tf` carries every stored overlay column by
> LAST-VALUE whenever the input frame has it, so the on-demand option TF read
> (`fno_resample.resample_session_to_tf`) and every other consumer inherit it
> automatically. The SAME PR also shipped the Phase-3a `/db` DISPLAY of the
> STORED overlay: the F&O candle browser's column picker offers the 10
> `GREEKS_OVERLAY_FIELDS` (`db_view.FNO_GREEKS_COLUMNS` +
> `FNO_GREEKS_COLUMN_TYPES` typed-NULL substitution for a legacy pre-Greeks
> partition tree — honest NULL cells, never a binder-error 500; `db.html`
> `FNO_GREEKS_COLS`/`curCols()`), which SUPERSEDES this section's original
> read-time-derived `_fno_derived_rows` sketch for /db — the STORED columns
> ARE the /db surface post-§15 (generation-time compute-and-store). The
> `/movers` delta/iv cells remain Phase 3b, deliberately NOT folded in.)*

When the 19 derived intraday TFs + `1d` are resampled from the stored
1-minute bars (`resample_1m_to_tf` on the movers / display path;
`spot_daily_resample` for the daily anchor — DEC-1MINUTE-ONLY-DERIVE-1DAY-001
#853), the Greeks / IV columns propagate by **LAST-VALUE (close-snapshot)
resampling — a derived N-minute bar's `iv/delta/gamma/theta/vega/rho` (+ the
provenance stamp) are EXACTLY the stored values of its LAST constituent
1-minute bar.** NEVER summed, NEVER averaged, NEVER OHLC-aggregated:

- **Greeks are STATE, not FLOW.** The rule is the same one the bar's `close`
  already obeys. It is mathematically identical to recomputing the kernel at
  the derived bar's close instant — same option close, same same-minute spot,
  same time-to-expiry — so it is zero-recompute, O(1) per derived bar, and
  bit-identical across every TF (one kernel, one stored tuple, one snapshot
  rule).
- **NA propagates honestly.** A derived bar whose last constituent 1-minute
  bar carries `iv = NULL` (solver non-convergence, missing spot, FUT leg —
  §15.8) carries the SAME honest NULL + stamp; never backfilled from an
  earlier constituent minute (that would be a stale-spot fabrication). A
  derived bar with zero constituent bars does not exist (the resampler emits
  no bar), so there is no fill case.
- **IV OHLC aggregation is NOT provided.** Only the close-snapshot is
  defined; an intrabar IV high/low/open series is explicitly out of scope
  (no consumer, and it would invite averaging misuse).
- **Test obligation (for the implementing PR):** property tests that for
  random 1-minute windows, every derived-TF bar's Greeks equal its last
  constituent 1-minute bar's Greeks, and that NA propagates.

# ADR-0012 — NIFTY dynamic futures brute-force strategy system (3 Fib-anchor variants)

**Decision ID:** `DEC-NIFTY-DYNAMIC-STRATEGY-001` (MIGRATION.md entry #1006)
**Date:** 2026-06-29
**Status:** ACCEPTED — design lock. Per Golden Rule #9 a 4-agent hostile review is run on THIS ADR before any engine code lands (the build PRs follow).
**Author:** Claude Code (operator-directed)
**Supersedes (clarifies):** none — net-new strategy track, scoped to NIFTY only.
**Builds on:** DEC-SHARED-CORE-001 (MIGRATION.md #441 — shared `trading-core` crate), DEC-DHAN-1M-ONLY-001 (1-minute base + deterministic resample), DEC-VENDOR-VOLUME-NORMALIZE-001 (`drop_volume=is_index`, `brutex/adapters/groww/adapter.py:217`), DEC-PLUGIN-001 (mask-width transition is ADR-gated), DEC-DASHBOARD-RESULTS-PAGE-001 (the `/results` page this folds away).

**Operator directive (verbatim intent):**
> "as of now we are going to do a thorough drill down deep brute force check only for nifty spot indices futures and options alone ... never ever fuck with me"
> "we will pick futures only especially for this nifty ... so now we can have the Vwap ... session based intraday level Vwap"
> "three unique strategies where every fibonacci level is unique ... every strategy should have all these also vwap ema 20 and ema 200 pivot points standard and candle close above or high and body level check and even wick level checks"
> "why only till r1 r2 r3 take r4 r5 and s4 s5 also ... previous day high and low should always be there"
> "intraday ... 3.20 pm is the forced exit time ... none should go overnight ... we should have sl and tp and without sl and tp also"
> "instead of making it as static we can entirely make it as dynamic common run-time scalable incremental approach"
> "i need only dashboard operator webpage ... only that should have the precise results and all the combos"

---

## 1. Context

BruteX is an extreme drill-down brute-force backtesting engine (CLAUDE.md §1). Today the engine sweeps a 52-condition vocabulary packed into a `u64` bitmask (bits 0-51 used; bits 52-63 reserved — `docs/TRACK1_VOCABULARY.md:45,47`), detecting a combo per bar with the O(1) test `(strategy_mask & bar_mask) == strategy_mask` (`docs/TRACK1_VOCABULARY.md:11`). Indicators and the 52-vocab are the SHARED `trading-core` crate (v0.3.0, lib `trading_core`; `rust/trading-core/Cargo.toml:14-27`) consumed bit-identically by BruteX (PyO3 wheel) and TickVault (native) per DEC-SHARED-CORE-001 (MIGRATION.md #441).

The operator has scoped a focused, high-conviction research track: **NIFTY only — spot + futures + options.** The brute-force engine runs on **NIFTY futures** because, unlike a spot index (which carries no traded volume — `drop_volume=instrument.is_index`, `brutex/adapters/groww/adapter.py:217`, `parser.py:98,144`, DEC-VENDOR-VOLUME-NORMALIZE-001), a futures contract HAS volume, so a session-anchored intraday **VWAP is feasible**. Futures backtests are **frictionless signal-only** (precise entry/exit, NO costs — spot/futures indices are not the tradable retail surface in this track); only the **options** translation carries brokerage / slippage / lot economics.

Three current-state facts the build PRs must respect (verified, not assumed):
- **Pivots stop at R4/S4 today.** `brutex/indicators/pivots.py` defines P, R1-R4, S1-S4 only (`pivots.py:1,50,80`); R5/S5 do NOT exist yet and must be ADDED.
- **Fibonacci is anchored to prev-day High/Low only today.** `brutex/indicators/fib_levels.py:1,13-27` produces 17 levels from the PDH/PDL range; the rolling-weekly and weekly-static anchors do NOT exist yet and must be ADDED.
- **Expiry is resolved from the instrument-master `expiry_date` field**, not parsed ad-hoc from the symbol string (`brutex/adapters/groww/instruments.py:206`); the near-month roll a trade entering on a date would use is `resolve_current_expiry(...)` (`brutex/options/executor.py:740`). Futures historical lookback defaults to 60 days before expiry (DEC-FUTURES-LOOKBACK-60D-001, `brutex/adapters/groww/contract_bounds.py:117`).

## 2. Decision

**D1 — Scope: NIFTY only.** Brute-force engine = **NIFTY futures**, frictionless signal-only (precise entry/exit, zero costs). Options track translates the winning futures signals into options with full costs / slippage / lot economics. Spot is the indicator substrate where a futures series is unavailable; VWAP/EMA/pivots/Fib are computed on the futures series.

**D2 — Three strategies, distinguished ONLY by the Fibonacci anchor.** (a) **prev-day OHLC** anchor (extends the existing `fib_levels.py`), (b) **rolling-weekly** anchor (the trailing 5-session High/Low range, recomputed each session), (c) **weekly-static** anchor (last completed calendar week's High/Low range, frozen for the current week). Both directions (long/short) for each. Everything else is identical across the three — the anchor is the only axis of difference, so the three are directly comparable.

**D3 — Shared condition set (every strategy carries all of these).** VWAP (futures, session-anchored intraday), EMA20 & EMA200 position, standard pivots **P, R1-R5, S1-S5** (R5/S5 newly added), prev-day High/Low always, **close / body / wick** level checks against the Fib + pivot levels, **BOS / CHoCH** market-structure, and a curated set of candle patterns.

**D4 — Vocabulary growth + mask width.** Extend the shared 52-condition vocab with: VWAP-position conditions, the R5/S5 pivot-proximity conditions, and BOS/CHoCH structure conditions. Counting against the current 52 (`docs/TRACK1_VOCABULARY.md:45`), this lands in the ~58-66 condition range — at or just past the `u64` ceiling, consuming all of the 12 reserved bits (52-63). To honor Golden Rule #11 (DYNAMIC — "add a new indicator/condition without re-architecting") we adopt a **`u128` mask** for genuine headroom (~64 free bits after this growth). This mask-width transition is **ADR-gated by DEC-PLUGIN-001 (this ADR is that gate)** — it is NOT silent. The O(1) per-bar evaluation is preserved: a `u128` AND is two `u64` ANDs, still constant-time (NOT an O(1)-total claim — O(1) per bar access). The **three Fib anchors are a swept STRATEGY parameter, NOT three sets of bits** — this is the single most important leanness decision: it keeps the vocab one fib group wide instead of tripling it.

**D5 — Intraday discipline.** Intraday only; **15:20 IST forced exit, no overrides ever** (`docs/TRACK1_BACKTEST_SPEC.md:242,740,765`; enforced by `brutex.calendars.nse.is_forced_exit_bar`, `brutex/engine/trade_sim.py:37-39,348,446`; Property P-09). Nothing held overnight. Two modes per combo: **Mode A** (SL/TP grid — a 40×50 = 2000-cell grid) and **Mode B** (no SL/TP, forced-exit-only — 1 cell). All 21 timeframes (1m base + 19 derived intraday + 1d).

**D6 — Output is a dynamic map, not a static strategy.** The deliverable is a **time-of-day × combo expectancy map** plus an **adaptive runtime selector** that, given the current minute-of-session and live conditions, picks the combo with the best forward expectancy. NOT one frozen rule-set.

**D7 — COMMON / dynamic / runtime / scalable / incremental, shared with TickVault.** All new conditions + indicators live in `trading-core` and are consumed bit-identically by BruteX (PyO3) and TickVault (native). Adding a future condition becomes a **single manifest edit + codegen** step (replacing today's ~7-site hand-edit), so the vocab is genuinely incremental. The frozen golden answer-key (`tests/golden/fixtures/vocab_golden_vectors.json`) is regenerated and both sides must satisfy it byte-for-byte.

**D8 — Full metadata stamping.** Every bar and every simulated trade is stamped: calendar date, weekday, minute-of-session, the contract's expiry date, and an **is-expiry-day** flag. This is what makes the time-of-day map and the expiry-day analysis possible, and it is captured / logged / audited (CLAUDE.md §2 Rule 8 hash-chained JSONL).

**D9 — ONE operator dashboard.** The `/` operator dashboard (v29.html, `brutex/dashboard/app.py:5165`) shows results + all combos + monitoring. The separate `/results` leaderboard page (`app.py:4174`, DEC-DASHBOARD-RESULTS-PAGE-001) is folded into `/` via the existing `/api/v1/sweep/leaderboard` API (`app.py:4326`) — **wire-first-then-delete** (wire the leaderboard into `/`, verify parity, THEN remove the `/results` route) so there is no regression window.

**D10 — The 9-standards funnel is the winner-guarantee.** No combo is ever called a "winner" without surviving the full statistical battery in `brutex/metrics/` + `engine/cascade.py` (Stage 1A→1B→2→3→4): DSR / PSR / SPA (White Reality Check + Hansen + Romano-Wolf) / CPCV (purge+embargo) / PBO-CSCV / walk-forward / lockbox holdout / Monte-Carlo permutation — and a reproducible 9-field blake3 run-id (`docs/TRACK1_BACKTEST_SPEC.md:743`). The funnel is precisely what collapses the trillions of brute-force trials down to the proven few.

## 3. Why these choices (rationale)

**Why futures, not spot, for VWAP.** A spot index has no traded volume by construction; the adapter zeroes it (`drop_volume=is_index`). VWAP needs volume. A NIFTY futures contract trades with real volume, so a session-anchored VWAP is a legitimate, computable level there — and futures are the closest frictionless proxy to the index for signal research.

**Why the Fib anchor is a parameter, not bits.** Tripling the 11-bit Fib group into three anchor-specific groups would add ~22 bits and explode the combo count for zero analytical gain — the three anchors are mutually exclusive per strategy. Modelling the anchor as a strategy-level swept parameter (exactly like `direction`, which is already a parameter not a bit — `docs/TRACK1_VOCABULARY.md:231`) keeps the vocab lean AND makes the three strategies directly comparable on identical machinery.

**Why u128 even though ~64 bits technically fits.** Fitting at exactly 64 leaves zero reserved headroom, which directly violates the DYNAMIC rule (Golden Rule #11). u128 costs almost nothing (two u64 ANDs, still O(1) per bar) and restores ~64 bits of future headroom. Because DEC-PLUGIN-001 forbids a silent mask-width change, this ADR is the explicit gate.

**Why keep BOS/CHoCH AND candles.** Both are cheap per-bar boolean conditions, and the 9-standards funnel (D10) is precisely the mechanism that discards conditions that add noise rather than edge — so there is no downside to admitting them into the vocab and letting the funnel adjudicate. Dropping them a priori would be a guess; admitting them and measuring is the honest, data-driven path.

**Why a dynamic time-of-day map beats a static strategy.** Intraday edge is famously time-of-day dependent (open drive, lunch chop, afternoon trend, pre-close). A single static rule averages across regimes and loses. A time-of-day × combo expectancy map keeps each regime's best combo and the runtime selector switches between them — this is the operator's "dynamic common run-time scalable incremental" directive made concrete.

## 4. Consequences

**Combo math (transparent, not fabricated).** With a frozen vocab of n conditions, the brute-force mask space is the sum of C(n,k) for k=2..5. At n≈64 that is ≈ 8.3 million masks. Per strategy the trial count is:

```
trials ≈ masks × 2 directions × 21 timeframes × 2 modes × (2000 Mode-A cells + 1 Mode-B cell)
       ≈ 8.3e6 × 2 × 21 × 2 × 2001  ≈ 1.4 × 10^12  (≈ 1.4 trillion / strategy)
```

× 3 strategies ≈ **4 trillion raw trials**. The EXACT figure is frozen only when the vocab + grid are locked in PR-1/PR-2; this ADR states the FORMULA, not a fake precise number. The 9-standards funnel + Bloom/LMDB O(1) dedup (CLAUDE.md §2 Rule 5) collapse this to the handful of statistically-proven winners — that collapse is the entire point.

**Honest proof posture (Golden Rule #10).** This ADR proves NOTHING about runtime — it is a design lock. The only runtime facts that are CI-PROVEN today are the O(1) scaling-ratio gates (p99 ratio ≤ 1.1) and the mask-AND microbench. The "2-4 days for ~33B trials on M4 Pro" SLA (CLAUDE.md §8) is a MODEL, not a measurement. A real, measured end-to-end benchmark gate lands in PR-8. No "millions in minutes" claim is made.

**Build sequence (each its own PR, 100% strict gates, hostile-reviewed):** PR-0 design lock (this) → PR-1 vocab+codegen (u128, shared) → PR-2 indicators (VWAP/EMA/pivots+R5S5/Fib 3-anchor/BOS-CHoCH/candles) → PR-3 futures roll + metadata stamping → PR-4 the 3 strategies + frictionless sim → PR-5 brute-force sweep + time-of-day map → PR-6 options translation (costs) → PR-7 dynamic selector + snooping guard → PR-8 dashboard consolidation + measured benchmark gate. PR-0/1/2 are data-independent (synthetic fixtures); PR-3+ consume the operator's NIFTY pull.

## 5. Phased implementation (smallest-first; each its own PR, 100% strict gates)

- **PR-0 — design lock (THIS ADR).** Docs only. DEC-NIFTY-DYNAMIC-STRATEGY-001 / MIGRATION.md #1006.
- **PR-1 — shared vocab + codegen.** u128 mask, manifest→codegen, new condition bits, regenerated golden vectors. Bit-identical BruteX + TickVault. Data-independent.
- **PR-2 — indicators.** VWAP (futures session), EMA20/200, pivots incl. R5/S5, Fib 3-anchor, BOS/CHoCH, curated candles. O(1) per-bar, look-ahead-guarded. Data-independent (synthetic fixtures).
- **PR-3 — futures roll + metadata.** Near-month roll via `resolve_current_expiry`, expiry from instrument-master `expiry_date`, per-bar stamping (date/weekday/minute/expiry-day). Consumes the pull.
- **PR-4 — 3 strategies + frictionless sim.** The Fib-anchor variants, both directions, signal-only futures sim (no costs).
- **PR-5 — brute-force sweep + time-of-day map.** u128 mask AND, Bloom+LMDB O(1) dedup, time-of-day × combo expectancy map. MEASURED mask-AND microbench + O(1) ratio gate.
- **PR-6 — options translation.** Winning signals → options with costs/slippage/lots.
- **PR-7 — dynamic selector + snooping guard.** Adaptive runtime selector + the 9-standards funnel.
- **PR-8 — dashboard consolidation + benchmark gate.** Fold leaderboard into `/`, wire-first-then-delete `/results`; add the MEASURED end-to-end benchmark gate.

## 6. Honest envelope (Golden Rule #10)

- ✅ **Design lock, scope-locked to NIFTY** — futures frictionless, options costed; verified against the current codebase (pivots R4/S4 today, Fib prev-day-only today, expiry from instrument-master — all cited file:line above), so the build PRs start from facts, not assumptions.
- ✅ **O(1) preserved** — u128 mask AND is two u64 ANDs, constant-time per bar; dedup stays Bloom+LMDB O(1); the claim is per-bar, not O(1)-total.
- ✅ **Shared with TickVault** — all new conditions/indicators in `trading-core`, golden-vector byte-equality is the contract.
- 🟡 **Runtime speed is UNPROVEN here** — this ADR is design only; the M4-Pro SLA is a model, not a measurement. Real measured benchmark is PR-8's gate. No fake throughput claim is made.
- 🟡 **Exact combo/trial count is a FORMULA, not a frozen number** — it locks when the vocab+grid freeze in PR-1/PR-2.
- 🚫 **No engine code in this PR** — docs-only; no brutex/ change, no scope expansion beyond NIFTY, no `/results` deletion yet (PR-8 does that wire-first-then-delete).

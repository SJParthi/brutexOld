# The Stage-4 gauntlet — all 9 "is this strategy real?" filters at a glance

> **Decision:** DEC-STAT-LOCKBOX-MC-001 (`docs/MIGRATION.md` #433) — the entry
> that completes the engine set. **Spec:** `TRACK1_BACKTEST_SPEC.md` §"Stage 4".
>
> Plain-English companion to the code. Every claim is backed by a module +
> a cited paper (CLAUDE.md Golden Rule #1 — no hallucination).

---

## The one-sentence problem

> *"My brute-force found a strategy that looks amazing. Before I trust real
> money to it, prove to me — nine different ways — that it isn't luck,
> overfitting, or the exit logic doing all the work."*

Stage 4 is the **final gauntlet**: nine independent statistical filters. A
candidate must survive **all** of them. Each catches a *different* way a
backtest can lie. As of #433, **all nine engines are built** (this PR added
the last two — Lockbox + Monte-Carlo).

---

## The 9 filters — what each one catches (the table)

| # | Filter | The lie it catches (plain English) | Pass rule | Built |
|---|---|---|---|---|
| 1 | Top-K composite | "keep only the genuinely best candidates" — a ranking pre-filter | keep top-K per group | ✅ (staging — the launcher's per-tuple `--top` truncation, default 20; 1,000 is the production-envelope sizing. See `TRACK1_BACKTEST_SPEC.md` §Stage-4 #1, DEC-SPEC-TOPK-RECONCILE-001) |
| 2 | **Deflated Sharpe** | "a high Sharpe is unremarkable once you tried thousands of strategies" | `DSR` clears the deflated bar | ✅ #430 |
| 3 | **CPCV** | "it only works on some train/test splits, not most" — general split/path-dependence resistance. **NOT a single-event killer** (verified 2026-07-18: a one-lucky-event fluke measured a 0.89 CPCV pass rate ≥ the 0.5 bar — 50% of `C(16,8)` folds structurally contain any one event's group, so the fluke passes most folds; the load-bearing anti-single-event filter is **DSR**, row 2, which red-flags it FAIL — DEC-EXTREME-EVENT-DOC-HONESTY-001) | positive Sharpe on enough of the `C(16,8)` folds | ✅ #428 |
| 4 | **Reality Check / SPA** | "the *best-of-N* beating the benchmark is just best-of-N luck" | SPA p-value `< 0.05` | ✅ #431 |
| 5 | **PBO** | "it's overfit — the in-sample best is below-median out-of-sample" | `PBO` probability low | ✅ #430 |
| 6 | **Romano-Wolf** | "which specific strategies are real?" — multiple-testing, not Bonferroni | in the FWER rejection set | ✅ #431 |
| 7 | **Walk-Forward ×3** | "it's curve-fit to one regime / needs a specific lookback" | positive Sharpe in ≥12/16 walks, all 3 variants | ✅ #428 |
| 8 | **Lockbox** | "it falls apart on data NO prior stage ever saw" | ≥500 held-out trades, Sharpe within 30% + MDD within 50% of training | ✅ **#433 (this PR)** |
| 9 | **Monte-Carlo** | "the EXIT logic does all the work — random entries would look just as good" | real Sharpe beats ≥99% of entry-shuffles (p `< 0.01`) | ✅ **#433 (this PR)** |

⭐ A strategy that survives all nine has been stress-tested against
data-snooping, overfitting, regime-dependence, held-out collapse, and
"is the signal even informative?" — the five ways backtests most often lie.

---

## The two new ones (this PR), in 20 seconds each

**#8 Lockbox** — set aside the most recent trades that **no earlier stage was
allowed to peek at**. Re-score the survivor there exactly once. If it needs
more than the default 6 months to gather 500 trades, the window quietly
extends backward (never into the training data). If it can't reach 500 trades
at all → rejected for *insufficient evidence*. If its Sharpe drops > 30% or
its drawdown grows > 50% vs training → rejected.

**#9 Monte-Carlo** — keep the exit logic, but throw the **entry signals at
random** 1,000 times. If the real strategy can't beat 99% of those random-entry
versions, the entry signal wasn't actually telling you anything — the exit grid
was doing the work. This is the *only* test that catches that failure mode.

---

## Guarantees — honest, no illusion

| Promise | Status | How |
|---|---|---|
| Same input + same seed → identical result, bit-for-bit | ✅ Guaranteed | Lockbox is pure (no RNG); Monte-Carlo uses one mandatory seed; golden + property tests |
| No `NaN` / `inf` ever leaks out | ✅ Guaranteed | every degenerate input raises a *clear error*; Lockbox returns `None` (not NaN) for an undefined Sharpe |
| No invented thresholds | ✅ Guaranteed | 6-month / 500-trade / 30% / 50% are the spec's institutional defaults; the MC p-value is the cited Phipson-Smyth (2010) estimator |
| 100% line + branch test coverage | ✅ Guaranteed | per-module CI gate `--cov-fail-under=100` (lockbox 96/32, monte_carlo 42/18) |
| Runs in O(1) time | 🚫 **No — and we say so** | Lockbox is `O(trades)`, Monte-Carlo is `O(1,000 re-runs)`; offline Stage-4 batch work, **not** a per-bar hot path |

> **O(1) honesty (Golden Rule #10):** the engine's *hot paths* (bar lookup,
> dedup, mask eval — billions of times) are O(1). A held-out re-score and a
> 1,000-shuffle permutation test are fundamentally not O(1) and are never
> claimed to be — claiming otherwise would be a hallucination.

---

## The cascade that chains all nine (DEC-STAT-CASCADE-001)

The nine engines are now tied into one end-to-end **gauntlet** by
`brutex/metrics/cascade.py` (`run_stage4_cascade`): hard rejection gates →
the nine filters in spec order **with short-circuit** (fail any → out, and we
record *which* filter + the stat) → composite ranking of the survivors. A
candidate must clear **all nine** to survive. It's pure + deterministic
(one seed), so the same pool always yields the same ranked survivor set.

| Order | Step | Level |
|---|---|---|
| 0 | Hard rejection gates (5 scalar gates) | per-candidate |
| 2-3 | Deflated Sharpe → CPCV | per-candidate |
| 4-6 | Reality Check / SPA → PBO → Romano-Wolf | **pool** (across survivors) |
| 7-9 | Walk-Forward ×3 → Lockbox → Monte-Carlo | per-candidate |
| — | Composite ranking → top survivors | per-candidate |

## What's left

The cascade *orchestration* is done. Still deferred (stated plainly, no
over-claim): the **production adapter** that feeds it real candidates — it
re-runs the Stage-2 trade-sim on the top leaderboard rows + reads the lake to
build each candidate's series (it needs real market bars) — and wiring the
filter outputs into the per-result `ResultMetrics` columns. Those are
separate, separately-tracked steps.

"""Track 1 brute-force engine.

Implements the 5-stage funnel from `docs/TRACK1_BACKTEST_SPEC.md`:

  Stage 1A — precompute 60 atomic conditions per bar per (instr × TF)
  Stage 1B — coarse Sharpe filter + drop bottom 50%
  Stage 2  — full backtest on survivors (~33B trials)
  Stage 2.5 — Bayesian adaptive early-stop
  Stage 3  — BIPOP-CMA-ES refinement on top-K
  Stage 4  — DSR / CPCV / Romano-Wolf / walk-forward
  Stage 4.7 — Lockbox (6mo + adaptive ≥500 trades per DEC-LOCK-001)

## Build status (2026-06-03)

Currently exposes:
- ``combo_mask`` — bitmask iteration + evaluation primitives (u64-limb —
  bits 0..59 fit u64 today; the u128 mask law + the two-limb fallback are
  documented in ``combo_mask.py``) (Stage-2 supermask kernel facade)
- ``stage1a`` — the Stage-1A bitset-matrix builder: drives the existing
  60-condition :func:`brutex.vocabulary.evaluator.evaluate_atoms` across a
  ``(instrument, interval)`` bar feed to produce the per-bar ``bar_bits``
  matrix the supermask kernel consumes (DEC-ENGINE-DRIVER-001).
- ``results_store`` — the deterministic sweep→Parquet results sink keyed by
  the BLAKE3 run-identity: idempotent, byte-reproducible writes with an O(1)
  read-back lookup + top-K leaderboard (DEC-RESULT-STORE-001).
- ``lake_slice`` — the ONE COMMON lake-slice read (catalog-reconcile → L1
  heal → window-bounded feed → keep-FIRST dedup) shared by the cascade
  replay and the reference-stamp wiring (DEC-VIX-STAMP-WIRE-001).
- ``reference_wire`` — the India-VIX stamper provider (lazy, once per run,
  honest ``None`` degrade) + the pure per-combo ``aggregate_trade_vix``
  mean/min/max fold. India VIX stays REFERENCE ONLY (CLAUDE.md §2 Rule 3).
- ``trade_sim`` — the Stage-2 spot trade-simulation walker: consumes a
  ``bool``-per-bar entry-signal stream + a ``CandleBar`` feed and emits
  int64-paisa :class:`~brutex.metrics.types.ClosedTrade` round-trips
  (signal-only — no slippage / brokerage / costs) for
  :func:`brutex.metrics.compute.compute_metrics` (DEC-TRADESIM-001).

Coming in subsequent commits:
- ``stage1b`` — coarse score filter
- ``stage2``  — full sweep orchestrator
- ``runner``  — end-to-end driver

Each subsequent module lands on its own PR with 100% line + branch
coverage per Golden Rule #12 per-module strict gate.

## Public API (forward-compat re-exports)

Re-exports the combo-mask primitives + the Stage-1A matrix builder. As
subsequent modules land, their public symbols will be re-exported here.
"""

from brutex.engine import (
    combo_mask,
    lake_slice,
    reference_wire,
    results_report,
    results_store,
    stage1a,
    trade_sim,
)
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    VOCAB_BITS,
    bits_to_mask,
    count_combos,
    evaluate_mask,
    iter_masks_k_of_bits,
    iter_masks_k_of_n,
    mask_to_bits,
    popcount,
)
from brutex.engine.lake_slice import load_lake_slice
from brutex.engine.reference_wire import (
    VIX_LAKE_SYMBOL,
    aggregate_trade_vix,
    vix_stamper_provider,
    warn_stale_vix_derived,
)
from brutex.engine.results_report import results_show_report
from brutex.engine.results_store import (
    FlushResult,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
)
from brutex.engine.stage1a import (
    ANCHOR_DEPENDENT_MASK,
    Stage1AMatrixBuilder,
    build_bar_bits,
)
from brutex.engine.trade_sim import (
    DEFAULT_FILL_POLICY,
    FillPolicy,
    LevelFill,
    SameBarTie,
    TradeSimWalker,
    simulate_trades,
)

__all__ = [
    "ANCHOR_DEPENDENT_MASK",
    "DEFAULT_FILL_POLICY",
    "MAX_VOCAB_BITS",
    "VIX_LAKE_SYMBOL",
    "VOCAB_BITS",
    "FillPolicy",
    "FlushResult",
    "LevelFill",
    "ResultRow",
    "ResultsStore",
    "ResultsStoreError",
    "SameBarTie",
    "Stage1AMatrixBuilder",
    "TradeSimWalker",
    "aggregate_trade_vix",
    "bits_to_mask",
    "build_bar_bits",
    "combo_mask",
    "count_combos",
    "evaluate_mask",
    "iter_masks_k_of_bits",
    "iter_masks_k_of_n",
    "lake_slice",
    "load_lake_slice",
    "mask_to_bits",
    "popcount",
    "reference_wire",
    "results_report",
    "results_show_report",
    "results_store",
    "simulate_trades",
    "stage1a",
    "trade_sim",
    "vix_stamper_provider",
    "warn_stale_vix_derived",
]

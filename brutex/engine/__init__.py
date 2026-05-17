"""Track 1 brute-force engine.

Implements the 5-stage funnel from `docs/TRACK1_BACKTEST_SPEC.md`:

  Stage 1A — precompute 52 atomic conditions per bar per (instr × TF)
  Stage 1B — coarse Sharpe filter + drop bottom 50%
  Stage 2  — full backtest on survivors (~33B trials)
  Stage 2.5 — Bayesian adaptive early-stop
  Stage 3  — BIPOP-CMA-ES refinement on top-K
  Stage 4  — DSR / CPCV / Romano-Wolf / walk-forward
  Stage 4.7 — Lockbox (6mo + adaptive ≥500 trades per DEC-LOCK-001)

## Build status (2026-05-17)

Foundation only. Currently exposes:
- ``combo_mask`` — u64 bitmask iteration + evaluation primitives
  (foundation for Stage 1A bar evaluation)

Coming in subsequent commits:
- ``stage1a`` — precompute + bar_bits packing
- ``stage1b`` — coarse score filter
- ``stage2``  — full sweep orchestrator
- ``runner``  — end-to-end driver

Each subsequent module lands on its own PR with 100% line + branch
coverage per Golden Rule #12 per-module strict gate.

## Public API (forward-compat re-exports)

Currently exposes the combo-mask primitives. As subsequent modules
land, their public symbols will be re-exported here.
"""

from brutex.engine import combo_mask
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    VOCAB_BITS,
    bits_to_mask,
    count_combos,
    evaluate_mask,
    iter_masks_k_of_n,
    mask_to_bits,
    popcount,
)

__all__ = [
    "MAX_VOCAB_BITS",
    "VOCAB_BITS",
    "bits_to_mask",
    "combo_mask",
    "count_combos",
    "evaluate_mask",
    "iter_masks_k_of_n",
    "mask_to_bits",
    "popcount",
]

"""The 52-condition evaluator — a thin marshalling shim over the shared Rust core.

Takes an `IndicatorSnapshot` (all per-bar indicator values) plus proximity
parameters and prior-bar history, and returns a u64 bitmask where bit `i` is set
iff condition `i` is true.

Per `docs/TRACK1_VOCABULARY.md` definitions. As of `DEC-SHARED-CORE-001`
Phase 2b (`docs/MIGRATION.md` #441) the 52-condition logic itself lives ONCE, in
the Rust `trading-core` crate (`rust/trading-core/`), and is consumed by BOTH
BruteX (this repo, via the PyO3 `trading_core` wheel) AND TickVault (native
Rust). This module no longer reimplements the conditions — it marshals the
snapshot into the flat primitive ABI and delegates to
`trading_core.evaluate_with_levels`, so the two engines are bit-identical by
construction (the COMMON / zero-duplication rule, CLAUDE.md §11). The frozen
400-case golden answer-key (`tests/golden/fixtures/vocab_golden_vectors.json`)
is the contract both sides satisfy bit-for-bit.

Each condition is a pure function of inputs — no internal state — so the
evaluator is trivially streaming-safe (live tick path) AND batch-safe (backtest
path). Output is bit-identical regardless of which path called it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import trading_core

from brutex.indicators.fib_levels import FibLevels
from brutex.indicators.pivots import PivotLevels

# ────── Input dataclasses ──────


@dataclass(frozen=True)
class IndicatorSnapshot:
    """All per-bar indicator values consumed by the evaluator.

    Money values are int64 paisa. Timestamps are timezone-aware IST.
    None values represent indicators that are still warming up (e.g.,
    EMA before bar 19/199).
    """

    # Bar OHLC + timestamp (IST timezone)
    timestamp_ist: datetime
    open_paisa: int
    high_paisa: int
    low_paisa: int
    close_paisa: int

    # EMA values (may be None during warm-up)
    ema20_paisa: int | None
    ema200_paisa: int | None

    # Standard Pivot Points for the day
    pivots: PivotLevels

    # Static Daily Fibonacci levels
    fib: FibLevels

    # Running day extremes (current bar's values incorporated)
    day_high_paisa: int
    day_low_paisa: int

    # Today's open price (computed at session start, constant intraday)
    open_of_day_paisa: int


@dataclass(frozen=True)
class ProximityParams:
    """Proximity tolerances (in basis points) for `near_*` conditions.

    `near` means `abs(close - level) <= close * bps / 10000`.

    Brute-force engine iterates over bps ∈ {5, 10, 25, 50}.
    """

    pivot_proximity_bps: int = 10
    pdh_pdl_proximity_bps: int = 10
    fib_proximity_bps: int = 10


@dataclass
class PriorBarHistory:
    """Rolling history of the last N bars' bullish/bearish flags.

    Used by Group 6 (prior bar pattern) conditions. The strategy
    engine maintains one of these per (instrument, TF) pair and passes
    `prior_n` + `flags` straight through to the Rust core, which owns the
    all-bullish / all-bearish / alternating logic (DEC-SHARED-CORE-001 #441).

    `prior_n` = the look-back count parametrised ∈ {1, 2, 3, 4}.
    """

    prior_n: int = 3
    flags: list[bool] = field(default_factory=list)  # True = bullish, False = bearish

    def update(self, is_bullish: bool) -> None:
        """Record this bar's bullish/bearish flag. O(1) per call."""
        self.flags.append(is_bullish)
        if len(self.flags) > self.prior_n:
            self.flags = self.flags[-self.prior_n :]


# ────── The 52-condition evaluator (delegates to the shared Rust core) ──────


def evaluate_atoms(
    snapshot: IndicatorSnapshot,
    proximity: ProximityParams,
    prior_history: PriorBarHistory,
) -> int:
    """Evaluate all 52 atomic conditions on one bar via the Rust `trading-core`.

    Marshals the pre-built indicator levels into the flat primitive ABI and
    delegates to `trading_core.evaluate_with_levels`. The pivot/fib levels are
    passed verbatim (no recompute), so hand-built levels — including a gap-day
    snapshot with `pdc == 0` — flow through unchanged; the Rust evaluator's own
    `pdc > 0` guard handles them. Bit-identical to the frozen golden oracle (the
    400-case answer-key) and to TickVault's native call of the same crate.

    Args:
        snapshot: All per-bar indicator values (OHLC + EMA + Pivots +
                  Fib + Day Hi/Lo + open_of_day).
        proximity: Tolerances for `near_*` conditions.
        prior_history: Rolling N-bar bullish/bearish flags for Group 6.

    Returns:
        u64 bitmask with bits 0..51 set per condition. Bits 52-63 always 0.

    O(1) per bar: one cache-resident Rust call, no allocations beyond the two
    small fixed-length level lists, no I/O.
    """
    pv = snapshot.pivots
    fib = snapshot.fib

    # Order MUST match trading_core.evaluate_with_levels:
    # [p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc] (len = PIVOT_LEVELS_LEN = 10).
    pivot_levels = [
        pv.p_paisa,
        pv.r1_paisa,
        pv.r2_paisa,
        pv.r3_paisa,
        pv.s1_paisa,
        pv.s2_paisa,
        pv.s3_paisa,
        pv.pdh_paisa,
        pv.pdl_paisa,
        pv.pdc_paisa,
    ]
    # [retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl,
    #  up127, up162, up200, up262, dn127, dn162, dn200, dn262]
    # (len = FIB_LEVELS_LEN = 15).
    fib_levels = [
        fib.retrace_level_at_pdh_paisa,
        fib.retrace_23_paisa,
        fib.retrace_38_paisa,
        fib.retrace_50_paisa,
        fib.retrace_62_paisa,
        fib.retrace_78_paisa,
        fib.retrace_level_at_pdl_paisa,
        fib.ext_up_127_paisa,
        fib.ext_up_162_paisa,
        fib.ext_up_200_paisa,
        fib.ext_up_262_paisa,
        fib.ext_down_127_paisa,
        fib.ext_down_162_paisa,
        fib.ext_down_200_paisa,
        fib.ext_down_262_paisa,
    ]

    # Group 8 reads only the IST wall-clock time-of-day (never the UTC instant);
    # seconds-of-day floors sub-second, matching the golden fixture's parser.
    ts = snapshot.timestamp_ist
    tod_seconds = ts.hour * 3600 + ts.minute * 60 + ts.second

    mask: int = trading_core.evaluate_with_levels(
        tod_seconds,
        snapshot.open_paisa,
        snapshot.high_paisa,
        snapshot.low_paisa,
        snapshot.close_paisa,
        snapshot.ema20_paisa,
        snapshot.ema200_paisa,
        pivot_levels,
        fib_levels,
        snapshot.day_high_paisa,
        snapshot.day_low_paisa,
        snapshot.open_of_day_paisa,
        proximity.pivot_proximity_bps,
        proximity.pdh_pdl_proximity_bps,
        proximity.fib_proximity_bps,
        prior_history.prior_n,
        prior_history.flags,
    )
    return mask

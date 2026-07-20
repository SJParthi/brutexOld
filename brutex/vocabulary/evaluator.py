"""The 66-condition evaluator — a thin marshalling shim over the shared Rust core.

Takes an `IndicatorSnapshot` (all per-bar indicator values) plus proximity
parameters and prior-bar history, and returns a u128 bitmask where bit `i` is set
iff condition `i` is true. The vocabulary currently declares bits 0..65 (66
conditions — 64 ACTIVE: the VWAP bits 52/53 are RETIRED tombstones; the
SuperTrend bits 64/65 are IMPLEMENTED as of the vocab-66 SuperTrend flip,
crate 0.5.0), with headroom to bit 127 (DEC-VOCAB-U128-CODEGEN-001).

Per `docs/TRACK1_VOCABULARY.md` definitions. As of `DEC-SHARED-CORE-001`
Phase 2b (`docs/MIGRATION.md` #441) the condition logic itself (52 then, 60,
now 66 — DEC-VOCAB-U128-CODEGEN-001 grew the vocabulary) lives ONCE, in
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
from brutex.indicators.market_structure import MarketStructureSignal
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

    # NOTE: the former ``vwap_paisa`` field (Group-10 VWAP bits 52/53) was
    # RETIRED with the VWAP tombstone (the spot-only engine has no traded
    # volume; the Rust ``VwapState`` was deleted from the crate at 0.4.0).
    # Bits 52/53 hold their positions forever and never fire.

    # Resolved BOS/CHoCH market-structure flags (Group 12, bits 56-59), produced by
    # the look-ahead-clean shared Rust trading_core.MarketStructureState — the
    # single source since DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001
    # (DEC-NIFTY-BOS-CHOCH-001) — threaded in like the resolved PriorBarHistory
    # flags. Defaulted to an all-false signal so every pre-existing snapshot caller
    # keeps working unchanged (the Group-12 bits then simply do not fire).
    market_structure: MarketStructureSignal = field(default_factory=MarketStructureSignal)

    # The SuperTrend line (Group 14, bits 64/65) in the ×2-SCALED paisa domain
    # (line × 2 exactly — the hl2 source is a half-paisa quantity), produced by
    # the shared Rust ``trading_core.SupertrendState`` (ATR-10 Wilder, factor 3,
    # Pine-v6 parity). ``None`` = warm-up (the first 9 bars) / no state →
    # bits 64/65 BOTH clear. Defaulted so every pre-existing snapshot caller
    # keeps working unchanged (the Group-14 bits then simply do not fire).
    supertrend_x2_paisa: int | None = None


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


# ────── The 66-condition evaluator (delegates to the shared Rust core) ──────


def pivot_levels_list(pivots: PivotLevels) -> list[int]:
    """The FROZEN 12-int ``evaluate_with_levels`` pivot ABI order (``PIVOT_LEVELS_LEN``).

    ``[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]`` — R5/S5 appended in
    PR-2, never renumbered. Extracted from :func:`evaluate_atoms`'s inline
    build (DEC-STAGE1A-RUST-BATCH-001) so the per-bar oracle marshal and the
    batch prepass's day tables are the SAME CODE — the order-drift class is
    killed at the root (CLAUDE.md §11 COMMON). Byte-identical delegation
    (the frozen 400-case golden + the per-module gate re-pin it).

    Parameters
    ----------
    pivots
        The day's :class:`~brutex.indicators.pivots.PivotLevels`.

    Returns
    -------
    list[int]
        The 12 pivot levels (int64 paisa) in the frozen evaluator ABI order.
    """
    return [
        pivots.p_paisa,
        pivots.r1_paisa,
        pivots.r2_paisa,
        pivots.r3_paisa,
        pivots.s1_paisa,
        pivots.s2_paisa,
        pivots.s3_paisa,
        pivots.pdh_paisa,
        pivots.pdl_paisa,
        pivots.pdc_paisa,
        pivots.r5_paisa,
        pivots.s5_paisa,
    ]


def fib_levels_list(fib: FibLevels) -> list[int]:
    """The FROZEN 15-int ``evaluate_with_levels`` fib ABI order (``FIB_LEVELS_LEN``).

    ``[retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl, up127, up162, up200,
    up262, dn127, dn162, dn200, dn262]`` — deliberately DISTINCT from the
    19-entry ``FIB_COMPUTE_LIST_LEN`` compute projection (the evaluator never
    reads the bullish-retrace tail; DEC-FIB-BULLISH-RETRACE-001). Extracted
    from :func:`evaluate_atoms`'s inline build — see :func:`pivot_levels_list`
    for the one-source rationale (DEC-STAGE1A-RUST-BATCH-001).

    Parameters
    ----------
    fib
        The day's :class:`~brutex.indicators.fib_levels.FibLevels`.

    Returns
    -------
    list[int]
        The 15 fib levels (int64 paisa) in the frozen evaluator ABI order.
    """
    return [
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


def evaluate_atoms(
    snapshot: IndicatorSnapshot,
    proximity: ProximityParams,
    prior_history: PriorBarHistory,
) -> int:
    """Evaluate the 64 ACTIVE atomic conditions on one bar via the Rust `trading-core`.

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
        u128 bitmask over the 66-bit layout. The 64 IMPLEMENTED bits (0-51,
        54-65) are set per condition (PR-2 added pivot R5/S5 bits 54-55;
        DEC-NIFTY-BOS-CHOCH-001 added the BOS/CHoCH bits 56-59, resolved from
        ``snapshot.market_structure``; the CPR bits 60-63 are computed in-core
        from the snapshot's pivot levels; the SuperTrend bits 64/65 compare
        ``close × 2`` against ``snapshot.supertrend_x2_paisa`` strictly —
        equality or a ``None`` line leaves both clear). The RETIRED VWAP
        tombstones (52/53) are never set.

    O(1) per bar: one cache-resident Rust call, no allocations beyond the two
    small fixed-length level lists, no I/O.
    """
    # The two level lists are built by the SHARED frozen-ABI projections
    # (DEC-STAGE1A-RUST-BATCH-001) — the same code the batch prepass's day
    # tables use, so the marshal order lives in exactly one place.
    pivot_levels = pivot_levels_list(snapshot.pivots)
    fib_levels = fib_levels_list(snapshot.fib)

    # Group 8 reads only the IST wall-clock time-of-day (never the UTC instant);
    # seconds-of-day floors sub-second, matching the golden fixture's parser.
    # DERIVED by the shared Rust seam (DEC-TOD-SECONDS-RUST-SEAM-001) — never
    # inline `h*3600` arithmetic here, so the value is bit-identical to the
    # TickVault-native derivation by construction.
    ts = snapshot.timestamp_ist
    tod_seconds = int(trading_core.seconds_of_day(ts.hour, ts.minute, ts.second))

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
        snapshot.market_structure.bos_bullish,
        snapshot.market_structure.bos_bearish,
        snapshot.market_structure.choch_bullish,
        snapshot.market_structure.choch_bearish,
        snapshot.supertrend_x2_paisa,
    )
    return mask

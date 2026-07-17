"""The 74-condition evaluator — a thin marshalling shim over the shared Rust core.

Takes an `IndicatorSnapshot` (all per-bar indicator values) plus proximity
parameters and prior-bar history, and returns a u128 bitmask where bit `i` is set
iff condition `i` is true. The vocabulary currently declares bits 0..73 (74
conditions — 67 ACTIVE: the VWAP bits 52/53 are RETIRED tombstones, the
SuperTrend bits 64/65 are declared layout-only `implemented=false`, and bits
66-68 are the reserved DEC-GAP-MIDPOINT-CONDITIONS-001 placeholders dropped at
re-stack), with headroom to bit 127 (DEC-VOCAB-U128-CODEGEN-001).

Per `docs/TRACK1_VOCABULARY.md` definitions. As of `DEC-SHARED-CORE-001`
Phase 2b (`docs/MIGRATION.md` #441) the condition logic itself (52 then 60,
then 66, now 74 — DEC-FIB-TEMPLATE-EXPRESS-001 grew it last) lives ONCE, in
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

    # The last k-confirmed swing HIGH/LOW levels (int64 paisa) for the
    # Swing-Proximity bits 72/73 (DEC-FIB-TEMPLATE-EXPRESS-001), read from the
    # SAME look-ahead-clean ``trading_core.MarketStructureState`` AFTER its
    # per-bar ``update`` (a swing is confirmed only k bars later, all at index
    # <= N). Defaulted ``None`` — every pre-existing snapshot caller keeps
    # working and the two bits simply do not fire (never fabricated); a
    # contract-roll hard reset returns them to ``None`` automatically.
    last_swing_high_paisa: int | None = None
    last_swing_low_paisa: int | None = None


#: The production default for :attr:`ProximityParams.swing_proximity_bps`
#: (10 bps — the sibling knobs' default). The ONE constant every default-omit
#: emission site compares against (CLAUDE.md §11 COMMON).
DEFAULT_SWING_PROXIMITY_BPS = 10


@dataclass(frozen=True)
class ProximityParams:
    """Proximity tolerances (in basis points) for `near_*` conditions.

    `near` means `abs(close - level) <= close * bps / 10000`.

    Brute-force engine iterates over bps ∈ {5, 10, 25, 50}.
    """

    pivot_proximity_bps: int = 10
    pdh_pdl_proximity_bps: int = 10
    fib_proximity_bps: int = 10
    #: Swing-proximity band for the near-swing bits 72/73
    #: (DEC-FIB-TEMPLATE-EXPRESS-001) — appended-last with the sibling default
    #: so every pre-existing construction keeps working; DEFAULT-OMITTED from
    #: every run-identity/digest emission (absent ⟺ 10, the clean bijection —
    #: see :func:`swing_params_fragment`).
    swing_proximity_bps: int = DEFAULT_SWING_PROXIMITY_BPS


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


# ────── The 74-condition evaluator (delegates to the shared Rust core) ──────


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
    """The 19-int ``evaluate_with_levels`` fib ABI order (``FIB_LEVELS_LEN``).

    ``[retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl, up127, up162, up200,
    up262, dn127, dn162, dn200, dn262, bull23, bull78, up424, dn424]`` — the
    original 15 slots byte-identical, the four DEC-FIB-TEMPLATE-EXPRESS-001
    slots APPENDED in the PINNED order (bits 69-71 read them). Deliberately
    DISTINCT from the ``FIB_COMPUTE_LIST_LEN`` compute projection: the two are
    numerically equal AGAIN (both 19) yet remain two different lists with
    different slot orders (this one carries NO bull_38/62) — never merge.
    Extracted from :func:`evaluate_atoms`'s inline build — see
    :func:`pivot_levels_list` for the one-source rationale
    (DEC-STAGE1A-RUST-BATCH-001).

    Parameters
    ----------
    fib
        The day's :class:`~brutex.indicators.fib_levels.FibLevels`.

    Returns
    -------
    list[int]
        The 19 fib levels (int64 paisa) in the evaluator ABI order.
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
        fib.retrace_bull_23_paisa,
        fib.retrace_bull_78_paisa,
        fib.ext_up_424_paisa,
        fib.ext_down_424_paisa,
    ]


def evaluate_atoms(
    snapshot: IndicatorSnapshot,
    proximity: ProximityParams,
    prior_history: PriorBarHistory,
) -> int:
    """Evaluate the 67 ACTIVE atomic conditions on one bar via the Rust `trading-core`.

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
        u128 bitmask over the 74-bit layout. The 67 IMPLEMENTED bits (0-51,
        54-63, 69-73) are set per condition (PR-2 added pivot R5/S5 bits 54-55;
        DEC-NIFTY-BOS-CHOCH-001 added the BOS/CHoCH bits 56-59, resolved from
        ``snapshot.market_structure``; the CPR bits 60-63 are computed in-core
        from the snapshot's pivot levels; DEC-FIB-TEMPLATE-EXPRESS-001 added
        the Fibonacci-Extended bits 69-71 — bullish 23.6/78.6 + the 424
        extension, off the grown 19-slot fib list — and the Swing-Proximity
        bits 72-73 off the snapshot's resolved swing levels). The RETIRED VWAP
        tombstones (52/53), the declared-only SuperTrend bits (64/65) and the
        reserved gap placeholders (66-68) are never set.

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
        snapshot.last_swing_high_paisa,
        snapshot.last_swing_low_paisa,
        proximity.swing_proximity_bps,
    )
    return mask


def swing_params_fragment(proximity: ProximityParams | None) -> dict[str, int]:
    """The DEFAULT-OMIT run-identity fragment for the swept swing band.

    The ONE emission law every params/digest re-derivation site shares
    (CLAUDE.md §11 COMMON; the ``price_source_params_fragment`` template —
    DEC-NIFTY-PRICE-SOURCE-AXIS-001 Override 1): the ``swing_proximity_bps``
    key is emitted **iff** *proximity* is present AND its swing band differs
    from :data:`DEFAULT_SWING_PROXIMITY_BPS`. Absent ⟺ the default 10 (a clean
    bijection — ``proximity=None`` means every band is defaulted, so no key),
    keeping every pre-DEC-FIB-TEMPLATE-EXPRESS-001 identity byte-stable while
    a swept swing cell re-keys. The pre-existing ``proximity_bps`` 3-list
    layout is FROZEN — it never grows to 4 (growing it would re-key every
    existing non-``None``-proximity run).

    Parameters
    ----------
    proximity
        The tuple's proximity params, or ``None`` (all-default).

    Returns
    -------
    dict[str, int]
        ``{}`` at the default, else ``{"swing_proximity_bps": <bps>}``.
    """
    if proximity is not None and proximity.swing_proximity_bps != DEFAULT_SWING_PROXIMITY_BPS:
        return {"swing_proximity_bps": proximity.swing_proximity_bps}
    return {}

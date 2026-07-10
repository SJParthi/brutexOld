"""ORDER-SENSITIVITY DIFFERENTIAL GATE for the Slice-0 fold/combine decomposition.

DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 — the Verifier B checkpoint-1 audit C23
mandate (design pack §6, ``docs/design/sweep_core/DESIGN_FINAL_RUST_SWEEP_CORE.md``
on branch ``claude/rust-sweep-core``; MANDATORY, part of Slice-0's Definition
of Ready). The named Slice-0 gates — the frozen golden corpus, the
``float.fromhex`` exact-value pins, the weekly mutmut pair — are all
FIXED-INPUT gates: an UNFAITHFUL fold/combine decomposition of the SEQUENTIAL
Welford/Pébay moment accumulations (``brutex/metrics/compute.py`` module
contract; the third/fourth-central-moment online updates live in
``_accumulate_moments``) could pass every one of them on the fixture inputs
yet silently change production bytes on an input shape the fixtures never
exercise. This gate closes that hole with a hypothesis DIFFERENTIAL property::

    combine_metrics_from_folds(_fold_trades(trades, …), …)
        ≡  the VERBATIM pre-Slice-0 _compute_metrics_impl body (frozen oracle)

at ``struct.pack("<d", …)`` BYTE equality per float field (signed zeros +
every ULP distinguished — the #1531 burn-in comparison idiom; int fields
compare exactly), over hypothesis-GENERATED trade sequences that include the
four MANDATED classes:

(a) **adversarial ORDERINGS** — identity / sorted-ascending /
    sorted-descending / interleaved-sign / hypothesis-drawn permutations of
    the SAME multiset (the fold order the sequential Welford walk is
    sensitive to);
(b) **fold/combine SPLITS at every boundary** — the split index swept
    ``0..n`` over each generated sequence. HONEST scope note: the Slice-0
    fold layer admits ONLY whole-sequence folds (``TradeFolds`` carries no
    merge — partial-fold COMBINATION is Slice 2's Rust ``fold_block`` arm),
    so "in whatever decomposition the fold layer admits" is exercised as the
    PREFIX fold ``trades[:k]`` and SUFFIX fold ``trades[k:]`` each run
    independently through the composition at every ``k`` — ``k = 0`` /
    ``k = n`` produce the degenerate empty-side folds, routed through the
    composition root's zero-trade ``_empty_metrics`` arm;
(c) **single-element folds** (n = 1 chains) — every ``trades[i:i+1]`` slice;
(d) **the negative / zero / denormal pnl classes** — ``pnl_paisa < 0``,
    ``== 0``, and float-denormal-producing magnitude ratios: pnl ``±1`` over
    a ``2**62`` start-equity base yields the smallest int64-paisa-reachable
    return magnitudes (~2e-19) feeding the moment accumulator, and the
    drawdown-percentage / span inputs include genuinely SUBNORMAL floats
    (``5e-324`` / ``2.2e-308``) feeding the combine-side divisions. Truly
    subnormal MOMENTS are unreachable from int64-paisa inputs — stated
    honestly, mirroring the golden corpus's ``denormal_adjacent`` note.

The oracle arm is the frozen test-local copy in
``tests/golden/test_compute_fold_combine_parity.py`` (imported, never
re-minted — CLAUDE.md §11 COMMON), captured VERBATIM from origin/main
``95955a71`` so the comparison is never decomposition-vs-itself.

INDEPENDENCE SCOPE (Verifier B Slice-0 re-pin, 2026-07-10): that oracle is
independent at the BODY level ONLY — it imports the untouched leaf helpers
(``_accumulate_moments`` / ``_skewness`` / ``_excess_kurtosis`` /
``_percentile`` / ``_safe_ratio`` / ``_consecutive_runs``) from LIVE
``compute.py``, so a defect INSIDE a shared leaf hits both arms identically
and is invisible to this differential. Measured blindness example: an
injected Pébay-m3 delta-sign mutant in ``_accumulate_moments`` passed all
4 properties here, killed only by the golden suite's ``float.fromhex``
exact-value pins. Correct AND disclosed for Slice-0 (the leaf helpers were
not moved; these gates verify the SPLIT) — but NOT fully independent.

This gate is the Slice-0 instance of the design §7 O2 cross-arm oracle
discipline; the Slice-2 Rust ``fold_block`` arm must therefore verify
against a GENUINELY independent oracle (frozen-bytes golden corpus or a
hand-rolled reference implementation — the design §6 Slice-2
Definition-of-Ready mandate on ``claude/rust-sweep-core``), never one
importing leaf helpers from live ``compute.py``, and never the "SAME
oracle through the §5 burn-in" shortcut this docstring previously
described.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from brutex.metrics.compute import (
    _compute_metrics_impl,
    _fold_trades,
    combine_metrics_from_folds,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.golden.test_compute_fold_combine_parity import (
    _oracle_metrics_pre_slice0,
    _pack_metrics,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_TS = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)

# ── Input strategies (the four mandated classes baked into the draws) ──

#: Per-trade P&L mixing the mandated classes: broad int64-paisa magnitudes,
#: a dense band around zero (negative / zero / positive), and exact class
#: representatives incl. the i64-scale huge-magnitude corpus values.
_PNL = st.one_of(
    st.integers(min_value=-5_000_000, max_value=5_000_000),
    st.integers(min_value=-3, max_value=3),
    st.sampled_from([0, 1, -1, 10**13, -(10**13)]),
)

#: Start equity: small (coarse floor-division returns), realistic, and the
#: 2**62 base whose ±1-paisa returns are the smallest int64-paisa-reachable
#: magnitudes (the denormal-adjacent class).
_START_EQUITY = st.sampled_from([999, 1_000_000, 10**9, 2**62])

#: Span years: subnormal (2.2e-308 — the OverflowError→inf→cap arm), tiny,
#: and ordinary finite spans.
_SPAN = st.one_of(
    st.sampled_from([2.2e-308, 1e-300, 0.001]),
    st.floats(min_value=1e-6, max_value=200.0, allow_nan=False, allow_infinity=False),
)

_BENCH = st.one_of(
    st.sampled_from([0.0, -0.0, 100.0, -50.0]),
    st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False),
)

_TOTAL_BARS = st.integers(min_value=1, max_value=10**6)

#: Drawdown 4-tuple (max_dd_paisa, max_dd_pct, max_dd_bars, ulcer) —
#: percentages include genuinely SUBNORMAL floats feeding calmar/mar.
_DRAWDOWN = st.tuples(
    st.integers(min_value=0, max_value=10**13),
    st.one_of(
        st.sampled_from([0.0, 5e-324, 2.2e-308]),
        st.floats(min_value=0.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
    ),
    st.integers(min_value=0, max_value=2000),
    st.one_of(
        st.sampled_from([0.0, 5e-324]),
        st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
    ),
)


def _trade(pnl: int, mfe: int, mae: int, hold: int) -> ClosedTrade:
    """Build a minimal :class:`ClosedTrade` for the generated corpus."""
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=mfe,
        mae_paisa=mae,
        holding_bars=hold,
        entry_ts=_TS,
        exit_ts=_TS,
        direction=Side.BUY,
    )


_TRADE = st.builds(
    _trade,
    pnl=_PNL,
    mfe=st.integers(min_value=0, max_value=8000),
    mae=st.integers(min_value=0, max_value=8000),
    hold=st.integers(min_value=1, max_value=375),
)

_TRADES = st.lists(_TRADE, min_size=1, max_size=10)
_TRADES_MAYBE_EMPTY = st.lists(_TRADE, min_size=0, max_size=8)


def _assert_differential(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    bench: float,
    start_equity: int,
    span: float,
    drawdown: tuple[int, float, int, float],
) -> None:
    """Assert the composition is byte-identical to the frozen oracle.

    Compares BOTH the composition root (``_compute_metrics_impl``, which
    owns the zero-trade arm) and — for non-empty inputs — the LITERAL
    mandated form ``combine_metrics_from_folds(_fold_trades(trades, …), …)``
    against the pre-Slice-0 oracle at packed-byte equality.
    """
    expected = _pack_metrics(
        _oracle_metrics_pre_slice0(
            trades,
            total_bars,
            bench,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
    )
    via_impl = _pack_metrics(
        _compute_metrics_impl(
            trades,
            total_bars,
            bench,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
    )
    assert via_impl == expected
    if trades:
        via_composition = _pack_metrics(
            combine_metrics_from_folds(
                _fold_trades(list(trades), start_equity),
                total_bars=total_bars,
                benchmark_return_pct=bench,
                start_equity_paisa=start_equity,
                span_years=span,
                drawdown=drawdown,
            )
        )
        assert via_composition == expected


def _sort_key(trade: ClosedTrade) -> tuple[int, int, int, int]:
    """Deterministic total order for building the adversarial orderings."""
    return (trade.pnl_paisa, trade.mfe_paisa, trade.mae_paisa, trade.holding_bars)


def _interleaved_sign(trades: Sequence[ClosedTrade]) -> list[ClosedTrade]:
    """Alternate losers (ascending) with non-losers (descending) — the
    interleaved-sign permutation of the SAME multiset."""
    losers = sorted((t for t in trades if t.pnl_paisa < 0), key=_sort_key)
    rest = sorted((t for t in trades if t.pnl_paisa >= 0), key=_sort_key, reverse=True)
    out: list[ClosedTrade] = []
    for i in range(max(len(losers), len(rest))):
        if i < len(losers):
            out.append(losers[i])
        if i < len(rest):
            out.append(rest[i])
    return out


class TestOrderSensitivityDifferential:
    """(a) Adversarial orderings of the same multiset, differentially checked."""

    @settings(max_examples=100, deadline=None)
    @given(
        trades=_TRADES,
        total_bars=_TOTAL_BARS,
        bench=_BENCH,
        start_equity=_START_EQUITY,
        span=_SPAN,
        drawdown=_DRAWDOWN,
        data=st.data(),
    )
    def test_orderings_of_same_multiset_all_bit_identical_to_oracle(
        self,
        trades: list[ClosedTrade],
        total_bars: int,
        bench: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
        data: st.DataObject,
    ) -> None:
        orderings: list[list[ClosedTrade]] = [
            list(trades),
            sorted(trades, key=_sort_key),
            sorted(trades, key=_sort_key, reverse=True),
            _interleaved_sign(trades),
            data.draw(st.permutations(trades), label="drawn_permutation"),
        ]
        for ordering in orderings:
            assert sorted(ordering, key=_sort_key) == sorted(trades, key=_sort_key)
            _assert_differential(ordering, total_bars, bench, start_equity, span, drawdown)


class TestSplitBoundaryDifferential:
    """(b) Splits swept 0..n + (c) single-element folds, differentially checked."""

    @settings(max_examples=100, deadline=None)
    @given(
        trades=_TRADES_MAYBE_EMPTY,
        total_bars=_TOTAL_BARS,
        bench=_BENCH,
        start_equity=_START_EQUITY,
        span=_SPAN,
        drawdown=_DRAWDOWN,
    )
    def test_every_split_boundary_prefix_and_suffix_bit_identical(
        self,
        trades: list[ClosedTrade],
        total_bars: int,
        bench: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        for k in range(len(trades) + 1):
            _assert_differential(trades[:k], total_bars, bench, start_equity, span, drawdown)
            _assert_differential(trades[k:], total_bars, bench, start_equity, span, drawdown)

    @settings(max_examples=100, deadline=None)
    @given(
        trades=_TRADES,
        total_bars=_TOTAL_BARS,
        bench=_BENCH,
        start_equity=_START_EQUITY,
        span=_SPAN,
        drawdown=_DRAWDOWN,
    )
    def test_single_element_folds_bit_identical(
        self,
        trades: list[ClosedTrade],
        total_bars: int,
        bench: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        for i in range(len(trades)):
            _assert_differential(trades[i : i + 1], total_bars, bench, start_equity, span, drawdown)


class TestPnlClassDifferential:
    """(d) The negative / zero / denormal-adjacent pnl classes, concentrated."""

    @settings(max_examples=100, deadline=None)
    @given(
        pnls=st.lists(st.sampled_from([-1, 0, 1, -3, 7]), min_size=1, max_size=8),
        total_bars=_TOTAL_BARS,
        bench=_BENCH,
        span=_SPAN,
        drawdown=_DRAWDOWN,
    )
    def test_unit_pnl_over_2pow62_base_bit_identical(
        self,
        pnls: list[int],
        total_bars: int,
        bench: float,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        """±1-paisa pnl over a 2**62 base — the smallest reachable returns
        (~2e-19) feed the Welford/Pébay walk; zero and negative classes are
        guaranteed present in the sampled alphabet."""
        trades = [_trade(p, abs(p), abs(p), 1) for p in pnls]
        _assert_differential(trades, total_bars, bench, 2**62, span, drawdown)
        # The same alphabet over the coarse 999 base exercises the
        # floor-division negative-expectancy class differentially.
        _assert_differential(trades, total_bars, bench, 999, span, drawdown)

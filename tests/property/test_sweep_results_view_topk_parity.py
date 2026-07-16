"""Order-parity pins for the fix-5 bounded /results top-k rank.

DEC-MEMORY-FOOTPRINT-HARDENING-001 (fix 5): ``build_sweep_leaderboard``'s
non-preset arm replaced the per-request whole-list two-pass sort
(``sorted(key=run_id)`` then stable ``sort(key=value, reverse=True)`` —
O(rows·log rows)) with ONE bounded ``heapq.nsmallest`` over the composite key
``(-value, run_id)`` (O(rows·log depth)). This suite is the parity CONTRACT:
for adversarial metric sets — ties, ``±0.0``, int64 paisa beyond ``2**53``,
equal values differing only in ``run_id``, duplicate run_ids (equal
composites, where stability in scan order decides) — the new path's order is
byte-identical to the VERBATIM pre-fix two-pass law, at multiple depths.

HONEST limit (stated at the call site too): a NaN metric has no total order
under EITHER path, so NaN is outside the parity claim — the store writers
persist finite metrics (the gates' fail-closed ``non_finite_input`` law).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("polars", reason="sweep_results_view imports the ResultsStore reader stack")

from pathlib import Path

from brutex.dashboard.sweep_results_view import SweepScan, build_sweep_leaderboard
from brutex.engine.results_store import ResultRow

#: lake_root is UNUSED whenever ``scan=`` is injected (no store load runs).
_UNUSED_LAKE = Path("/nonexistent-lake-root-never-read")


def _row(
    marker: int,
    *,
    run_seed: int,
    sharpe: float = 1.0,
    net_pnl_paisa: int = 100_00,
) -> ResultRow:
    """A valid :class:`ResultRow`; ``total_trades`` carries ``1000 + marker``.

    The marker rides a projected column that is NOT the ranked one in any
    test here, so two rows sharing BOTH the rank value AND the run_id (the
    stability-residue case) stay distinguishable in the projected payload
    (``winning + losing <= total`` stays satisfied for every marker).
    """
    return ResultRow(
        run_id=f"{run_seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=0,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=1000 + marker,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=sharpe,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _scan(rows: list[ResultRow]) -> SweepScan:
    """A minimal injected scan (rank-parity needs only the rows)."""
    return SweepScan(
        rows=tuple(rows),
        store_count=1,
        skipped=0,
        run_id_conflicts=0,
        eras={},
        instruments=("NIFTY",),
        timeframes=("5m",),
        directions=("bull",),
        snapshot_hashes=(),
    )


def _verbatim_pre_fix_order(
    rows: list[ResultRow], *, rank_by: str, depth: int
) -> list[tuple[str, int]]:
    """The EXACT pre-fix two-pass law, reproduced verbatim as the oracle."""
    ordered = sorted(rows, key=lambda r: r.run_id)
    ordered.sort(key=lambda r: getattr(r, rank_by), reverse=True)
    return [(r.run_id, r.total_trades - 1000) for r in ordered[:depth]]


def _served_order(rows: list[ResultRow], *, rank_by: str, depth: int) -> list[tuple[str, int]]:
    """The order the fixed reader actually serves (projected identity pairs)."""
    board = build_sweep_leaderboard(_UNUSED_LAKE, rank_by=rank_by, top_n=depth, scan=_scan(rows))
    return [(row["run_id"], row["total_trades"] - 1000) for row in board.rows]


#: Small run_seed pool → duplicate run_ids are LIKELY (the equal-composite
#: stability case); tie-heavy value pools → equal values differing only in
#: run_id are likely too.
_RUN_SEEDS = st.integers(min_value=0, max_value=5)

_FLOATS = st.one_of(
    st.sampled_from([0.0, -0.0, 1.0, -1.0, 2.5, 2.5]),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
)

#: int64 paisa incl. the exact >2**53 float-precision traps the raw-int rank
#: law exists for (adjacent giants a float cast would collapse); bounded to
#: the store's signed-int64 row validation range.
_INT64_MAX = 2**63 - 1
_INTS = st.one_of(
    st.sampled_from([2**53, 2**53 + 1, -(2**53) - 1, -(2**53), 0, 0, 100_00]),
    st.integers(min_value=-_INT64_MAX, max_value=_INT64_MAX),
)

_DEPTHS = st.sampled_from([1, 2, 3, 5, 17, 50, 500])


class TestTopKOrderParity:
    """new-path order == verbatim-old-path order, byte-for-byte."""

    @settings(deadline=None)
    @given(
        values=st.lists(st.tuples(_FLOATS, _RUN_SEEDS), min_size=0, max_size=40),
        depth=_DEPTHS,
    )
    def test_float_metric_parity(self, values: list[tuple[float, int]], depth: int) -> None:
        rows = [
            _row(marker, run_seed=seed, sharpe=value) for marker, (value, seed) in enumerate(values)
        ]
        assert _served_order(rows, rank_by="sharpe_ratio", depth=depth) == (
            _verbatim_pre_fix_order(rows, rank_by="sharpe_ratio", depth=depth)
        )

    @settings(deadline=None)
    @given(
        values=st.lists(st.tuples(_INTS, _RUN_SEEDS), min_size=0, max_size=40),
        depth=_DEPTHS,
    )
    def test_int_paisa_metric_parity(self, values: list[tuple[int, int]], depth: int) -> None:
        rows = [
            _row(marker, run_seed=seed, net_pnl_paisa=value)
            for marker, (value, seed) in enumerate(values)
        ]
        assert _served_order(rows, rank_by="net_pnl_paisa", depth=depth) == (
            _verbatim_pre_fix_order(rows, rank_by="net_pnl_paisa", depth=depth)
        )

    def test_adversarial_tie_golden(self) -> None:
        """Hand-built worst case: value ties + ±0.0 + duplicate run_ids + big ints.

        Expected sequence derived BY HAND from the pre-fix law: value DESC,
        run_id ASC within a value tie, scan order within a full
        (value, run_id) tie; ``-0.0 == 0.0`` so the zero pair falls to run_id.
        """
        rows = [
            _row(0, run_seed=3, sharpe=1.0),  # tie value, higher run_id
            _row(1, run_seed=1, sharpe=1.0),  # tie value, lower run_id → first of the pair
            _row(2, run_seed=2, sharpe=0.0),  # +0.0
            _row(3, run_seed=1, sharpe=-0.0),  # -0.0 == 0.0 → run_id decides (lower)
            _row(4, run_seed=4, sharpe=2.0),  # unique top
            _row(5, run_seed=2, sharpe=1.0),  # full (value, run_id) tie with marker 6
            _row(6, run_seed=2, sharpe=1.0),  # ... scan order keeps 5 before 6
        ]
        expected_markers = [4, 1, 5, 6, 0, 3, 2]
        served = _served_order(rows, rank_by="sharpe_ratio", depth=7)
        assert [marker for _rid, marker in served] == expected_markers
        assert served == _verbatim_pre_fix_order(rows, rank_by="sharpe_ratio", depth=7)

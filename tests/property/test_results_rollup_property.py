"""Hypothesis properties for the rollup union trim (DEC-RESULTS-TOPK-ROLLUP-001).

The three laws the persisted union rests on, over randomized row sets:

* **Prefix containment / top-N equality** — for every union axis, the top-N
  (N ≤ k) of the FULL set under the exact two-pass total order equals the
  top-N of the trimmed union under the same order.
* **Trim monotonicity** — a row trimmed out of ``trim_to_union(rows, k)`` can
  never re-enter the union when MORE rows are added (additions only demote),
  so incremental union-and-trim converges on rebuild-from-scratch.
* **Fold-order independence** — the kept set is a pure function of the row
  SET, not of arrival order.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("polars", reason="ResultRow lives in the ResultsStore module")

from brutex.engine.results_rollup import trim_to_union, union_axes
from brutex.engine.results_store import ResultRow
from hypothesis import given, settings
from hypothesis import strategies as st


def _row_from(seed: int, x: int) -> ResultRow:
    wins = x % 50
    losses = (x >> 6) % 50
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1 + (x % 7),
        net_pnl_paisa=(x % 100_000) - 50_000,
        gross_profit_paisa=x % 90_000,
        gross_loss_paisa=(x >> 3) % 70_000,
        max_drawdown_paisa=(x >> 5) % 40_000,
        expectancy_paisa=((x >> 2) % 9_000) - 4_500,
        total_trades=wins + losses + (x % 3) + 1,
        winning_trades=wins,
        losing_trades=losses,
        sharpe_ratio=((x % 600) - 300) / 100,
        sortino_ratio=(((x >> 4) % 700) - 350) / 100,
        calmar_ratio=(((x >> 7) % 500) - 250) / 100,
        profit_factor=((x >> 2) % 400) / 100,
        win_rate=(x % 101) / 100,
        max_drawdown_pct=((x >> 9) % 100) / 100,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        # The four NULLABLE metrics-preset axes union_axes() self-extended to
        # since #1699 (DEC-METRICS-*) — x-distinct finite values so the prefix /
        # monotonicity / order-independence properties exercise those axes with
        # real data (a real sweep row always carries them).
        log_growth_at_half_kelly=((x % 3000) - 1500) / 1000,
        sl_hit_rate=(x % 101) / 100,
        trailing_sharpe_ratio=((x % 600) - 300) / 100,
        decay_weighted_net_paisa=float((x % 100_000) - 50_000),
    )


def _rows(values: list[int]) -> list[ResultRow]:
    return [_row_from(seed, x) for seed, x in enumerate(values, start=1)]


_VALUES = st.lists(st.integers(min_value=0, max_value=2**32 - 1), min_size=1, max_size=40)
_K = st.integers(min_value=1, max_value=8)


@settings(max_examples=60, deadline=None)
@given(values=_VALUES, k=_K)
def test_union_top_n_equals_full_top_n(values: list[int], k: int) -> None:
    """The prefix property over every axis — the served-row parity core."""
    rows = _rows(values)
    kept = list(trim_to_union(rows, k))
    for axis in union_axes():
        full = sorted(rows, key=lambda r: r.run_id)
        full.sort(key=lambda r: getattr(r, axis.field), reverse=axis.descending)
        union = sorted(kept, key=lambda r: r.run_id)
        union.sort(key=lambda r: getattr(r, axis.field), reverse=axis.descending)
        n = min(k, len(rows))
        assert [r.run_id for r in full[:n]] == [r.run_id for r in union[:n]]
    # And the run_id-ASC prefix (the composite / generic-tie cover).
    base = sorted(rows, key=lambda r: r.run_id)
    kept_ids = {r.run_id for r in kept}
    assert {r.run_id for r in base[:k]} <= kept_ids


@settings(max_examples=60, deadline=None)
@given(values=_VALUES, extra=_VALUES, k=_K)
def test_trim_monotonicity_additions_only_demote(
    values: list[int], extra: list[int], k: int
) -> None:
    """A trimmed-out row can never re-enter under further additions."""
    rows = _rows(values)
    more = [_row_from(seed, x) for seed, x in enumerate(extra, start=len(values) + 1)]
    kept_before = {r.run_id for r in trim_to_union(rows, k)}
    kept_after = {r.run_id for r in trim_to_union([*rows, *more], k)}
    original_ids = {r.run_id for r in rows}
    assert (kept_after & original_ids) <= kept_before


@settings(max_examples=60, deadline=None)
@given(values=_VALUES, k=_K)
def test_trim_is_order_independent(values: list[int], k: int) -> None:
    """The kept sequence is a pure function of the row SET."""
    rows = _rows(values)
    forward = trim_to_union(rows, k)
    backward = trim_to_union(list(reversed(rows)), k)
    assert forward == backward

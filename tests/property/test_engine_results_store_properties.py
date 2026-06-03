"""Property tests for the deterministic results store (`brutex.engine.results_store`).

Invariants (hold for any set of valid result rows):

* **Byte-determinism** (Golden Rule #4) — the same row set always serialises to
  a byte-identical Parquet file, independent of append order; re-flushing is
  byte-identical (Golden Rule #7).
* **Round-trip fidelity** — ``load(flush(store))`` recovers every row exactly,
  including full-precision metrics (NEVER rounded) and 2dp-snapped prices.
* **Price canonicalisation** — every stored price is exactly two decimals
  (DEC-SPOT-PRICE-2DP-001), and snapping is idempotent.
* **Leaderboard total-order** — ``top_k(k)`` is the length-``k`` prefix of the
  fully ranked order, so it is deterministic for any insertion order.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (mirrors tests/property/test_engine_stage1a_properties.py): the
# `@st.composite` strategies below are invoked with no args because the
# decorator injects `draw` at runtime; pyright does not model that rewrite.

_FINITE_FLOAT = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
_PRICE = st.decimals(
    min_value=Decimal(0), max_value=Decimal(10**7), allow_nan=False, allow_infinity=False
)


@st.composite
def _result_row(draw: st.DrawFn, run_id: str) -> ResultRow:
    """Draw one valid :class:`ResultRow` for *run_id*."""
    total = draw(st.integers(min_value=0, max_value=10_000))
    winning = draw(st.integers(min_value=0, max_value=total))
    losing = draw(st.integers(min_value=0, max_value=total - winning))
    return ResultRow(
        run_id=run_id,
        instrument=draw(st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"])),
        tf=draw(st.sampled_from(["1m", "5m", "15m", "1d"])),
        direction=draw(st.sampled_from(["bull", "bear"])),
        mode=draw(st.sampled_from(["A", "B"])),
        net_pnl_paisa=draw(st.integers(min_value=-(10**15), max_value=10**15)),
        gross_profit_paisa=draw(st.integers(min_value=0, max_value=10**15)),
        gross_loss_paisa=draw(st.integers(min_value=0, max_value=10**15)),
        max_drawdown_paisa=draw(st.integers(min_value=0, max_value=10**15)),
        expectancy_paisa=draw(st.integers(min_value=-(10**12), max_value=10**12)),
        total_trades=total,
        winning_trades=winning,
        losing_trades=losing,
        sharpe_ratio=draw(_FINITE_FLOAT),
        sortino_ratio=draw(_FINITE_FLOAT),
        calmar_ratio=draw(_FINITE_FLOAT),
        profit_factor=draw(_FINITE_FLOAT),
        win_rate=draw(_FINITE_FLOAT),
        max_drawdown_pct=draw(_FINITE_FLOAT),
        avg_entry_price=draw(_PRICE),
        avg_exit_price=draw(_PRICE),
    )


@st.composite
def _row_set(draw: st.DrawFn) -> list[ResultRow]:
    """Draw a list of 1–6 rows with distinct run-identities."""
    seeds = draw(
        st.lists(
            st.integers(min_value=0, max_value=2**256 - 1),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    return [draw(_result_row(f"{seed:064x}")) for seed in seeds]


@settings(max_examples=60, deadline=None)
@given(_row_set())
def test_determinism_independent_of_append_order(rows: list[ResultRow]) -> None:
    pytest.importorskip("polars")  # flush needs the [data] extra; skips the .[dev]-only job
    with tempfile.TemporaryDirectory() as tmp:
        path_a = Path(tmp) / "a.parquet"
        path_b = Path(tmp) / "b.parquet"
        store_a = ResultsStore(path=path_a)
        store_a.extend(rows)
        store_b = ResultsStore(path=path_b)
        store_b.extend(reversed(rows))  # opposite append order
        result_a = store_a.flush()
        result_b = store_b.flush()
        assert path_a.read_bytes() == path_b.read_bytes()
        assert result_a.content_hash == result_b.content_hash


@settings(max_examples=60, deadline=None)
@given(_row_set())
def test_reflush_is_byte_identical(rows: list[ResultRow]) -> None:
    pytest.importorskip("polars")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.parquet"
        store = ResultsStore(path=path)
        store.extend(rows)
        store.flush()
        first = path.read_bytes()
        store.flush()
        assert path.read_bytes() == first


@settings(max_examples=60, deadline=None)
@given(_row_set())
def test_round_trip_preserves_every_row(rows: list[ResultRow]) -> None:
    pytest.importorskip("polars")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.parquet"
        store = ResultsStore(path=path)
        store.extend(rows)
        store.flush()
        reloaded = ResultsStore.load(path)
        assert reloaded.rows() == store.rows()


@settings(max_examples=200, deadline=None)
@given(
    price=st.decimals(
        min_value=Decimal(0), max_value=Decimal(10**7), allow_nan=False, allow_infinity=False
    )
)
def test_price_is_always_two_decimals(price: Decimal) -> None:
    row = ResultRow(
        run_id=f"{1:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        net_pnl_paisa=0,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=price,
        avg_exit_price=price,
    )
    # round_money quantises to Decimal("0.01") → exponent exactly -2.
    assert row.avg_entry_price.as_tuple().exponent == -2


@settings(max_examples=200, deadline=None)
@given(metric=st.floats(allow_nan=False, allow_infinity=False))
def test_full_precision_metric_round_trips_exactly(metric: float) -> None:
    pytest.importorskip("polars")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.parquet"
        store = ResultsStore(path=path)
        store.append(
            ResultRow(
                run_id=f"{1:064x}",
                instrument="NIFTY",
                tf="5m",
                direction="bull",
                mode="A",
                net_pnl_paisa=0,
                gross_profit_paisa=0,
                gross_loss_paisa=0,
                max_drawdown_paisa=0,
                expectancy_paisa=0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                sharpe_ratio=metric,
                sortino_ratio=0.0,
                calmar_ratio=0.0,
                profit_factor=0.0,
                win_rate=0.0,
                max_drawdown_pct=0.0,
                avg_entry_price=Decimal("1.00"),
                avg_exit_price=Decimal("1.00"),
            )
        )
        store.flush()
        got = ResultsStore.load(path).get(f"{1:064x}")
        assert got is not None
        # Float64 preserves the exact IEEE-754 bits (metrics are NEVER rounded).
        assert got.sharpe_ratio == metric


@settings(max_examples=60, deadline=None)
@given(_row_set(), st.integers(min_value=1, max_value=10))
def test_top_k_is_prefix_of_full_ranking(rows: list[ResultRow], k: int) -> None:
    store = ResultsStore(path=Path("unused.parquet"))
    store.extend(rows)
    full = store.top_k(len(rows), by="sharpe_ratio")
    assert store.top_k(k, by="sharpe_ratio") == full[:k]


def _minimal_row(run_id: str, net_pnl_paisa: int) -> ResultRow:
    """A valid row that differs only by ``run_id`` + ``net_pnl_paisa``."""
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("1.00"),
        avg_exit_price=Decimal("1.00"),
    )


@settings(max_examples=50, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**256 - 1),
    pnl_a=st.integers(min_value=-(10**12), max_value=10**12),
    pnl_b=st.integers(min_value=-(10**12), max_value=10**12),
)
def test_same_run_id_different_result_always_conflicts(seed: int, pnl_a: int, pnl_b: int) -> None:
    # One run-identity must map to exactly one result — a different row under the
    # same id is a reproducibility breach and ALWAYS raises (never silent overwrite).
    assume(pnl_a != pnl_b)
    run_id = f"{seed:064x}"
    store = ResultsStore(path=Path("unused.parquet"))
    first_add = store.append(_minimal_row(run_id, pnl_a))
    assert first_add is True
    dup_add = store.append(_minimal_row(run_id, pnl_a))
    assert dup_add is False  # identical = no-op
    with pytest.raises(ResultsStoreError, match="conflict"):
        store.append(_minimal_row(run_id, pnl_b))

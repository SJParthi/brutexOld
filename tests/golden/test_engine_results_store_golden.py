"""Golden byte-equality oracle for the deterministic results store.

A small, fully hand-derived three-row fixture whose every snapped price,
leaderboard position and round-tripped value is locked below. Mirrors the
byte-reproducibility style of
``tests/integration/test_data_lake_real.py::test_byte_for_byte_reproducibility``
(two independent writers → identical bytes + identical sha256) — the repo pins
LOGICAL outputs, not version-fragile Parquet bytes (same philosophy as
``tests/golden/test_engine_stage1a_golden.py``). Any regression in the write
boundary (price snap, column order, schema, zstd settings, sort) breaks this
oracle (DEC-RESULT-STORE-001 / NFR-DET-001 bit-exactness, Golden Rule #4 / #7).

Fixture (3 rows, distinct BLAKE3 run-identities ``00…01`` / ``00…02`` / ``00…03``):

* **Row 1** — ``sharpe=1.5``; ``avg_entry=14020.7999`` (vendor float-noise) →
  ``14020.80`` (ROUND_HALF_UP); ``avg_exit=14100.005`` → ``14100.01``.
* **Row 2** — ``sharpe=3.25`` (the leader); ``avg_entry=250.10`` /
  ``avg_exit=249.95`` (already 2dp).
* **Row 3** — ``sharpe=0.75`` (the laggard); ``avg_entry=0`` → ``0.00`` /
  ``avg_exit=100`` → ``100.00``.

Hand-derived expectations
-------------------------
* Snapped prices: row1 ⇒ (14020.80, 14100.01); row3 ⇒ (0.00, 100.00).
* Leaderboard ``top_k`` by ``sharpe_ratio`` desc ⇒ ``[row2, row1, row3]``.
* Two independent stores holding these rows flush to byte-identical files.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from brutex.engine.results_store import ResultRow, ResultsStore


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(seed: int, sharpe: float, entry: str, exit_: str) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        net_pnl_paisa=1_000 * seed,
        gross_profit_paisa=2_000 * seed,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25 * seed,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal(entry),
        avg_exit_price=Decimal(exit_),
    )


#: The hand-derived canonical fixture (the oracle).
_ROW_1 = _row(1, 1.5, "14020.7999", "14100.005")
_ROW_2 = _row(2, 3.25, "250.10", "249.95")
_ROW_3 = _row(3, 0.75, "0", "100")
_FIXTURE = (_ROW_1, _ROW_2, _ROW_3)


def test_golden_price_snap() -> None:
    # ROUND_HALF_UP to the 2dp tick grid — locked literal expectations.
    assert (_ROW_1.avg_entry_price, _ROW_1.avg_exit_price) == (
        Decimal("14020.80"),
        Decimal("14100.01"),
    )
    assert (_ROW_3.avg_entry_price, _ROW_3.avg_exit_price) == (
        Decimal("0.00"),
        Decimal("100.00"),
    )


def test_golden_leaderboard_order() -> None:
    store = ResultsStore(path=Path("unused.parquet"))
    store.extend(_FIXTURE)
    leaders = store.top_k(3, by="sharpe_ratio")
    assert [r.run_id for r in leaders] == [_rid(2), _rid(1), _rid(3)]


def test_golden_byte_for_byte_reproducibility(tmp_path: Path) -> None:
    """Two independent stores with the fixture rows flush to identical bytes."""
    store_a = ResultsStore(path=tmp_path / "a.parquet")
    store_a.extend(_FIXTURE)
    store_b = ResultsStore(path=tmp_path / "b.parquet")
    store_b.extend(reversed(_FIXTURE))  # opposite append order → same bytes
    result_a = store_a.flush()
    result_b = store_b.flush()
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()
    assert result_a.content_hash == result_b.content_hash
    assert result_a.row_count == 3


def test_golden_round_trip_values(tmp_path: Path) -> None:
    """Round-trip recovers every locked field value, bit-exact."""
    path = tmp_path / "results.parquet"
    store = ResultsStore(path=path)
    store.extend(_FIXTURE)
    store.flush()
    reloaded = ResultsStore.load(path)
    assert reloaded.rows() == (_ROW_1, _ROW_2, _ROW_3)  # run_id-sorted
    got = reloaded.get(_rid(1))
    assert got is not None
    assert got.sharpe_ratio == 1.5  # full precision, never rounded
    assert got.avg_entry_price == Decimal("14020.80")
    assert got.net_pnl_paisa == 1_000

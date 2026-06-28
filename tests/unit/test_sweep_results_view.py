"""Unit tests for the sweep-leaderboard reader (DEC-DASHBOARD-RESULTS-PAGE-001).

Exercises :mod:`brutex.dashboard.sweep_results_view` directly (pure, no FastAPI):
the happy-path discovery + ranking, the optional filters, the honest empty state,
the rank-by fall-back + clamp, the cross-store run_id dedup, the malformed-file
skip (a real corrupt Parquet), and the money / full-precision-metric projection.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard.sweep_results_view import (
    DEFAULT_RANK_BY,
    DEFAULT_TOP_N,
    MAX_TOP_N,
    RANKABLE_COLUMNS,
    SweepLeaderboard,
    build_sweep_leaderboard,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.money import format_inr


def _run_id(seed: int) -> str:
    """A valid 64-char lowercase-hex run-identity from an int seed (deterministic)."""
    return f"{seed:064x}"


def _row(
    *,
    seed: int,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    net_pnl_paisa: int = 100_00,
    sharpe: float = 1.0,
    mask_bits: int = 0,
    total_trades: int = 10,
) -> ResultRow:
    """A minimal valid :class:`ResultRow` with the fields the leaderboard reads."""
    return ResultRow(
        run_id=_run_id(seed),
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=total_trades,
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


def _write_store(
    lake_root: Path, *, instrument: str, tf: str, direction: str, rows: list[ResultRow]
) -> Path:
    """Write a per-tuple store at ``<lake>/sweeps/<instrument>/<tf>/<direction>_k1_<digest>.parquet``."""
    dest = lake_root / "sweeps" / instrument / tf / f"{direction}_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def test_empty_when_no_sweeps_dir(tmp_path: Path) -> None:
    """No ``<lake>/sweeps`` directory → honest empty leaderboard (available False)."""
    board = build_sweep_leaderboard(tmp_path)
    assert isinstance(board, SweepLeaderboard)
    assert board.available is False
    assert board.rows == ()
    assert board.total_rows == 0
    assert board.store_count == 0
    assert board.skipped == 0
    assert board.instruments == ()
    assert board.timeframes == ()
    assert board.directions == ()


def test_ranks_descending_by_default_metric(tmp_path: Path) -> None:
    """Rows rank by Sharpe descending — best first — with sequential ranks."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[
            _row(seed=1, sharpe=0.5),
            _row(seed=2, sharpe=2.5),
            _row(seed=3, sharpe=1.5),
        ],
    )
    board = build_sweep_leaderboard(tmp_path)
    assert board.available is True
    assert board.store_count == 1
    assert board.rank_by == DEFAULT_RANK_BY
    sharpes = [r["sharpe_ratio"] for r in board.rows]
    assert sharpes == [2.5, 1.5, 0.5]
    assert [r["rank"] for r in board.rows] == [1, 2, 3]
    assert board.total_rows == 3


def test_filters_instrument_tf_direction_case_insensitive(tmp_path: Path) -> None:
    """The optional filters select exactly the matching rows (case-insensitive)."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    _write_store(
        tmp_path,
        instrument="BANKNIFTY",
        tf="15m",
        direction="bear",
        rows=[_row(seed=2, instrument="BANKNIFTY", tf="15m", direction="bear")],
    )
    board = build_sweep_leaderboard(tmp_path, instrument="nifty", tf="5M", direction="BULL")
    assert {r["instrument"] for r in board.rows} == {"NIFTY"}
    assert board.total_rows == 1
    # The dropdown option sets reflect EVERY discovered row, not just the filtered set.
    assert board.instruments == ("BANKNIFTY", "NIFTY")
    assert board.timeframes == ("15m", "5m")
    assert board.directions == ("bear", "bull")


def test_filter_tf_mismatch_excludes_row(tmp_path: Path) -> None:
    """A row matching the instrument but NOT the tf filter is excluded (tf branch)."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="15m")
    assert board.rows == ()
    assert board.total_rows == 0


def test_filter_direction_mismatch_excludes_row(tmp_path: Path) -> None:
    """A row matching instrument + tf but NOT the direction filter is excluded."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="5m", direction="bear")
    assert board.rows == ()
    assert board.total_rows == 0


def test_money_and_full_precision_projection(tmp_path: Path) -> None:
    """Net P&L is ``₹`` formatted; Sharpe / win-rate are passed through unrounded."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, net_pnl_paisa=123_456, sharpe=1.23456789)],
    )
    board = build_sweep_leaderboard(tmp_path)
    row = board.rows[0]
    assert row["net_pnl"] == format_inr(123_456)
    assert row["net_pnl_rupees"] == pytest.approx(1234.56)
    # Full precision preserved (NEVER rounded — ranking integrity).
    assert row["sharpe_ratio"] == 1.23456789
    assert row["win_rate"] == 0.6


def test_conditions_decoded_from_mask(tmp_path: Path) -> None:
    """A mask with set bits decodes to its condition names; mask 0 → empty string."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, mask_bits=0), _row(seed=2, mask_bits=0b11)],
    )
    board = build_sweep_leaderboard(tmp_path)
    by_mask = {r["mask_bits"]: r["conditions"] for r in board.rows}
    assert by_mask[0] == ""
    assert by_mask[0b11] != ""  # two condition names, joined


def test_top_n_clamped(tmp_path: Path) -> None:
    """``top_n`` is clamped into [1, MAX_TOP_N]."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=i, sharpe=float(i)) for i in range(1, 6)],
    )
    assert len(build_sweep_leaderboard(tmp_path, top_n=2).rows) == 2
    assert len(build_sweep_leaderboard(tmp_path, top_n=0).rows) == 1  # clamped up to 1
    big = build_sweep_leaderboard(tmp_path, top_n=MAX_TOP_N + 100)
    assert len(big.rows) == 5  # capped depth ≥ available rows → all 5


def test_unknown_rank_by_falls_back_to_default(tmp_path: Path) -> None:
    """An unknown ``rank_by`` degrades to the default metric, never an error."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, rank_by="not_a_column")
    assert board.rank_by == DEFAULT_RANK_BY


def test_rank_by_net_pnl(tmp_path: Path) -> None:
    """A money ranking column ranks largest-P&L first."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, net_pnl_paisa=100), _row(seed=2, net_pnl_paisa=900)],
    )
    board = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa")
    assert board.rank_by == "net_pnl_paisa"
    assert board.rows[0]["net_pnl_rupees"] > board.rows[1]["net_pnl_rupees"]


def test_dedup_same_run_id_across_stores(tmp_path: Path) -> None:
    """The same combo (run_id) in two stores is counted once."""
    shared = _row(seed=7)
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[shared])
    _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[shared])
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 2
    assert board.total_rows == 1  # deduped by run_id


def test_corrupt_store_is_skipped_not_crashed(tmp_path: Path) -> None:
    """A corrupt per-tuple Parquet SKIPS that store (counted), never 500s the read."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    bad = tmp_path / "sweeps" / "BANKNIFTY" / "15m" / "bull_k1_deadbeefdeadbeef.parquet"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a parquet file at all")
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 1  # the good one
    assert board.skipped == 1  # the corrupt one
    assert board.available is True
    assert {r["instrument"] for r in board.rows} == {"NIFTY"}


def test_raw_polars_error_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw polars error escaping the wrap is still SKIPPED (the belt-and-suspenders set)."""
    import polars as pl
    from brutex.dashboard import sweep_results_view as mod

    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])

    def _raise(_path: Path) -> ResultsStore:
        raise pl.exceptions.ComputeError("synthetic raw polars fault")

    monkeypatch.setattr(mod.ResultsStore, "load", classmethod(lambda cls, p: _raise(p)))
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 0
    assert board.skipped == 1
    assert board.available is False


def test_to_dict_shape(tmp_path: Path) -> None:
    """``to_dict`` carries every payload key the route + page expect."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    payload = build_sweep_leaderboard(tmp_path).to_dict()
    assert set(payload) == {
        "available",
        "total_rows",
        "store_count",
        "skipped",
        "rank_by",
        "instruments",
        "timeframes",
        "directions",
        "rows",
    }
    assert isinstance(payload["rows"], list)
    assert isinstance(payload["instruments"], list)


def test_module_constants() -> None:
    """The public constants are sane + the rank-by allow-list is non-empty."""
    assert DEFAULT_TOP_N == 50
    assert MAX_TOP_N == 500
    assert DEFAULT_RANK_BY in RANKABLE_COLUMNS
    assert "net_pnl_paisa" in RANKABLE_COLUMNS

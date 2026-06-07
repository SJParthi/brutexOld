"""Unit tests for the sweep results read-back (`brutex.engine.results_report`).

100% line + branch coverage of the operator leaderboard read-back
(DEC-SWEEP-WINNERS-BY-NAME-001):

* :func:`discover_result_files` — recursive sorted glob, the not-a-directory raise,
  and the no-files-yet empty result;
* :func:`load_results_tree` — empty tree → empty store, and a multi-file merge;
* :func:`render_leaderboard` — the empty placeholder, the deflated ``None`` → ``—``
  and float branches, and the no-condition vs named branches;
* :func:`results_show_report` — the header, top-N truncation, alias sorting, the
  empty-store branch, and the ``top < 1`` / unknown-sort guards.

A fixture builds a real per-tuple Parquet tree with
:class:`~brutex.engine.results_store.ResultsStore` so the read path is exercised
end-to-end (write → discover → load → render).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_report import (
    SORT_ALIASES,
    discover_result_files,
    load_results_tree,
    render_leaderboard,
    results_show_report,
)
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    seed: int,
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    mask_bits: int = (1 << 0) | (1 << 30),
    sharpe: float = 1.5,
    net_pnl_paisa: int = 1_000,
    deflated_sharpe: float | None = 0.97,
) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument=instrument,
        tf=tf,
        direction="bull",
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=2_000,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        deflated_sharpe=deflated_sharpe,
    )


def _write_tree(root: Path) -> None:
    """Write a 2-file results tree mirroring <root>/<instrument>/<tf>/<file>.parquet."""
    s1 = ResultsStore(path=root / "NIFTY" / "5m" / "bull_k2_aaaa.parquet")
    s1.append(_row(1, sharpe=3.25, net_pnl_paisa=9_000))
    s1.append(_row(2, sharpe=0.75, net_pnl_paisa=500, deflated_sharpe=None))
    s1.flush()
    s2 = ResultsStore(path=root / "BANKNIFTY" / "15m" / "bull_k1_bbbb.parquet")
    s2.append(_row(3, instrument="BANKNIFTY", tf="15m", sharpe=1.5, net_pnl_paisa=4_000))
    s2.flush()


# ──────────────────────────────────────────────────────────────────────────
# discover_result_files
# ──────────────────────────────────────────────────────────────────────────


class TestDiscover:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="results root not found"):
            discover_result_files(tmp_path / "nope")

    def test_root_is_a_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(ResultsStoreError, match="not a directory"):
            discover_result_files(f)

    def test_empty_root_returns_empty_list(self, tmp_path: Path) -> None:
        assert discover_result_files(tmp_path) == []

    def test_discovers_nested_files_sorted(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        files = discover_result_files(tmp_path)
        assert files == sorted(files)
        assert len(files) == 2


# ──────────────────────────────────────────────────────────────────────────
# load_results_tree
# ──────────────────────────────────────────────────────────────────────────


class TestLoadTree:
    def test_empty_tree_is_empty_store(self, tmp_path: Path) -> None:
        store = load_results_tree(tmp_path)
        assert isinstance(store, ResultsStore)
        assert len(store) == 0

    def test_merges_all_files(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        store = load_results_tree(tmp_path)
        assert len(store) == 3
        assert store.get(_rid(1)) is not None
        assert store.get(_rid(3)) is not None


# ──────────────────────────────────────────────────────────────────────────
# render_leaderboard
# ──────────────────────────────────────────────────────────────────────────


class TestRender:
    def test_empty_rows_placeholder(self) -> None:
        assert render_leaderboard([]) == "(no persisted results)"

    def test_named_conditions_and_deflated_value(self) -> None:
        out = render_leaderboard([_row(1, mask_bits=(1 << 0) | (1 << 23))])
        assert "#1  NIFTY 5m bull" in out
        assert "close_above_ema20 AND near_fib_618" in out
        assert "gate=PASS" in out
        assert "dsr=+0.9700" in out

    def test_no_condition_mask_and_deflated_none(self) -> None:
        out = render_leaderboard([_row(1, mask_bits=0, deflated_sharpe=None)])
        assert "(no conditions — enters every bar)" in out
        assert "dsr=—" in out


# ──────────────────────────────────────────────────────────────────────────
# results_show_report
# ──────────────────────────────────────────────────────────────────────────


class TestShowReport:
    def test_top_must_be_at_least_one(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="top must be >= 1"):
            results_show_report(tmp_path, top=0)

    def test_unknown_sort_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="unknown sort key"):
            results_show_report(tmp_path, top=5, sort_by="bogus")

    def test_empty_tree_reports_zero(self, tmp_path: Path) -> None:
        out = results_show_report(tmp_path, top=5)
        assert "files=0  finalists=0" in out
        assert "(no persisted results)" in out

    def test_ranked_by_sharpe_default(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=10)
        assert "finalists=3" in out
        # sharpe order: 3.25 (rid1) > 1.5 (rid3) > 0.75 (rid2) → rid1 first.
        first_line = out.split("#1")[1].splitlines()[0]
        assert "NIFTY 5m bull" in first_line
        assert "showing top 3" in out

    def test_top_truncates(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=1)
        assert "showing top 1" in out
        assert "#2" not in out

    def test_sort_by_pnl_alias(self, tmp_path: Path) -> None:
        _write_tree(tmp_path)
        out = results_show_report(tmp_path, top=10, sort_by="pnl")
        # pnl order: 9_000 (rid1) > 4_000 (rid3) > 500 (rid2).
        assert out.index("pnl=") > 0
        assert "sort=pnl" in out


def test_sort_aliases_resolve_to_real_columns(tmp_path: Path) -> None:
    # Every advertised alias must be a usable sort key end-to-end.
    _write_tree(tmp_path)
    for alias in SORT_ALIASES:
        out = results_show_report(tmp_path, top=10, sort_by=alias)
        assert f"sort={alias}" in out

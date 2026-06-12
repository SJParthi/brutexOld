"""Tests for the ``brutex results show`` command (DEC-SWEEP-WINNERS-BY-NAME-001).

``brutex/cli.py`` is measured at 100% line+branch (DEC-COV-CLI-HARNESS-001 —
no longer coverage-omitted). ``cmd_results_show`` carries real behaviour —
delegation to the engine read-back, the success print, and the clean
user-error path on a bad root — so it is unit-tested here for correctness.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex import cli
from brutex.engine.results_store import ResultRow, ResultsStore


def _write_tree(root: Path) -> None:
    store = ResultsStore(path=root / "NIFTY" / "5m" / "bull_k2_aaaa.parquet")
    store.append(
        ResultRow(
            run_id=f"{1:064x}",
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            mode="A",
            mask_bits=(1 << 0) | (1 << 23),
            net_pnl_paisa=9_000,
            gross_profit_paisa=10_000,
            gross_loss_paisa=1_000,
            max_drawdown_paisa=500,
            expectancy_paisa=225,
            total_trades=40,
            winning_trades=25,
            losing_trades=15,
            sharpe_ratio=3.25,
            sortino_ratio=2.0,
            calmar_ratio=1.25,
            profit_factor=1.8,
            win_rate=0.625,
            max_drawdown_pct=12.5,
            avg_entry_price=Decimal("100.00"),
            avg_exit_price=Decimal("101.00"),
            deflated_sharpe=0.97,
        )
    )
    store.flush()


def test_results_show_prints_named_winners(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_tree(tmp_path)
    rc = cli.cmd_results_show(results_root=tmp_path, top=20, sort_by="sharpe")
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "close_above_ema20 AND near_fib_618" in out
    # DEC-STAGE3-WIRE-001 (review F6): no per-row gate verdict — refined trees
    # hold UNGATED Stage-3 trial rows, so the header scopes the claim instead.
    assert "gate=PASS" not in out
    assert "UNGATED Stage-3" in out
    assert "finalists=1" in out


def test_results_show_missing_root_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.cmd_results_show(results_root=tmp_path / "nope", top=20, sort_by="sharpe")
    err = capsys.readouterr().err
    assert rc == cli.EXIT_USER_ERROR
    assert "results error" in err


def test_results_show_via_main_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_tree(tmp_path)
    rc = cli.main(["results", "show", "--results-root", str(tmp_path), "--top", "5"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "BruteX results" in out

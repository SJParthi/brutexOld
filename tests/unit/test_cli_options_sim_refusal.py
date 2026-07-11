"""Regression — ``cmd_options_sim`` refuses a NIFTY-futures series winner cleanly.

Review round-1 finding 9 shipped the refusal (``brutex/cli.py::cmd_options_sim``)
but round-2 finding 6(b) proved it landed with ZERO executing test — the fixed
bug (a raw ``ValueError`` traceback out of ``OptionsUnderlying(row.instrument)``
deep in the replay) had no fails-before/passes-after pin (Golden Rule #12).
This file is that pin: a persisted futures-series row driven through the REAL
``cmd_options_sim`` must exit ``EXIT_USER_ERROR`` with the PR-6 follow-up
one-liner on stderr — never a traceback, and never reaching the lake replay.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")

from brutex import cli
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL

_RUN_ID = f"{7:064x}"


def _write_futures_tree(root: Path) -> None:
    """Persist ONE futures-series winner row with the real store writer."""
    store = ResultsStore(
        path=root / NIFTY_FUTURES_SERIES_SYMBOL / "5m" / "bull_k1_deadbeefdeadbeef.parquet"
    )
    store.append(
        ResultRow(
            run_id=_RUN_ID,
            instrument=NIFTY_FUTURES_SERIES_SYMBOL,
            tf="5m",
            direction="bull",
            mode="A",
            mask_bits=1 << 52,  # a futures-track VWAP bit
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


def test_futures_series_winner_is_a_clean_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A futures-row run-id exits EXIT_USER_ERROR naming the PR-6 follow-up."""
    results_root = tmp_path / "sweeps"
    _write_futures_tree(results_root)
    rc = cli.cmd_options_sim(
        run_id=_RUN_ID,
        results_root=results_root,
        lake_root=tmp_path / "lake",  # never reached — the refusal fires first
        catalog_path=None,
        exchange="NSE",
        segment="FNO",
        start="2025-06-24",
        end="2025-08-05",
        expiry="weekly",
        broker="zerodha",
    )
    err = capsys.readouterr().err
    assert rc == cli.EXIT_USER_ERROR
    assert "NIFTY-futures series" in err
    assert NIFTY_FUTURES_SERIES_SYMBOL in err
    assert "PR-6" in err
    assert "not supported yet" in err


def test_missing_run_id_still_reads_as_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal sits AFTER the row lookup — an unknown id keeps its message."""
    results_root = tmp_path / "sweeps"
    _write_futures_tree(results_root)
    rc = cli.cmd_options_sim(
        run_id=f"{8:064x}",
        results_root=results_root,
        lake_root=tmp_path / "lake",
        catalog_path=None,
        exchange="NSE",
        segment="FNO",
        start="2025-06-24",
        end="2025-08-05",
        expiry="weekly",
        broker="zerodha",
    )
    err = capsys.readouterr().err
    assert rc == cli.EXIT_USER_ERROR
    assert "not found" in err

"""Unit test for the slimmed :mod:`brutex.dashboard.coverage_reconcile`.

DEC-WINDOW-LEDGER-001 (Stage d). The fetch-coverage reconcile band-aids
(``reconcile_windows`` / ``reconcile_coverage_with_lake`` /
``reconcile_empty_terminal`` / ``reconcile_failed_windows_for_repull``) were
RETIRED once the unified :class:`~brutex.data.window_ledger.WindowLedger` became
the sole skip authority — its per-window record + the ledger cold-start
(:func:`~brutex.data.window_ledger_store.reconcile_ledger_with_lake`) replace
every coarse coverage-set patch. The only survivor is :func:`load_lake_spans`
(the read-only lake span reader the ledger cold-start consumes), a thin DuckDB
I/O surface exercised end-to-end by the ledger's integration tests.
"""

from __future__ import annotations

from pathlib import Path

from brutex.dashboard.coverage_reconcile import load_lake_spans


def test_load_lake_spans_returns_a_list(tmp_path: Path) -> None:
    # Smoke: an empty lake (no datasets) yields an empty span list, never raises —
    # whether DuckDB is present (empty read) or absent (ImportError degrade).
    spans = load_lake_spans(tmp_path)
    assert isinstance(spans, list)


def test_load_lake_spans_stays_unfiltered_over_a_stock_bearing_lake(tmp_path: Path) -> None:
    """The R2 third-consumer pin (DEC-UNIVERSE-INDEX-ONLY-001, design §6#6).

    The window-ledger COLD-START seed depends on the FULL walk (the twice-
    pinned db_view contract): under the shipped index4 default (the conftest
    forces ``BRUTEX_FEED_SCOPE=index4``) a stock-bearing lake's spans MUST
    still include the stock dataset — R2's ``admit`` kwarg is wrapper-threaded
    only, never the walk default — so a fresh-clone ledger keeps inheriting
    every already-stored window as SUCCESS (no quota blowout).
    """
    import pytest

    pytest.importorskip("duckdb", reason="the span reader needs duckdb")
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    lake = tmp_path / "lake"
    for symbol, segment in (("NIFTY", "INDEX"), ("RELIANCE", "CASH")):
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment=segment,
            symbol=symbol,
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=datetime(2024, 3, 1, 9, 15, tzinfo=UTC),
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100.5"),
                    volume=10,
                    open_interest=None,
                )
            ],
        )
    spans = load_lake_spans(lake)
    assert {span["symbol"] for span in spans} == {"NIFTY", "RELIANCE"}

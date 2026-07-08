"""End-to-end proof for ``brutex sweep`` (DEC-SWEEP-CLI-001).

The CLI shell is measured at 100% line+branch (DEC-COV-CLI-HARNESS-001 — no
longer coverage-omitted), and this INTEGRATION test is the honest validation
that the command actually loads real
bars from a Parquet lake, runs the assembled funnel, and prints the ranked report
— not just that the pure core works. It persists a tiny lake with
``ParquetWriter`` / ``ParquetBarSink`` (the same writers ingest uses), then runs
the command and inspects stdout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex import cli
from brutex.cli import cmd_sweep, main
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


@pytest.fixture(autouse=True)
def _inert_sweep_logging_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the dispatch-level logging guard inert here.

    ``main([... "sweep" ...])`` now configures PROCESS-GLOBAL structlog state
    (DEC-SWEEP-LOG-VOLUME-GUARD-001); stubbing it out keeps this file's
    assertions byte-identical and leak-free — the guard's own behavior is
    pinned in ``tests/unit/test_cli_sweep_log_level.py``.
    """
    monkeypatch.setattr(cli, "_configure_sweep_logging", lambda: None)


_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(6):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def test_sweep_loads_lake_and_prints_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        start="2024-01-02",
        end="2024-01-03",
        direction="BUY",
        k=2,
        conditions="0,1,2",
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=10,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The report actually rendered from lake-loaded bars (6 of them).
    assert "BruteX sweep — NIFTY 1minute BUY" in out
    assert "bars=6" in out
    assert "combos=3" in out  # C(3, 2)


def test_sweep_argv_path_prints_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Exercise the full argparse + dispatch via main(argv).
    lake_root, catalog_path = _persist(tmp_path)
    rc = main(
        [
            "sweep",
            _SYMBOL,
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--exchange",
            _EXCHANGE,
            "--segment",
            _SEGMENT,
            "--interval",
            _INTERVAL,
            "--k",
            "1",
            "--conditions",
            "5,6",
            "--sort-by",
            "pnl",
            "--oos",
            "--lake-root",
            str(lake_root),
            "--catalog",
            str(catalog_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "BruteX sweep — NIFTY 1minute BUY" in out
    assert "sort=pnl" in out  # the --sort-by flag reaches the report
    assert "Out-of-sample" in out  # the --oos flag reaches the report


def test_sweep_no_bars_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    # A window with no data → honest message + non-zero exit, never a crash.
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        start="2030-01-01",
        end="2030-01-02",
        direction="BUY",
        k=2,
        conditions=None,
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=20,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 1
    assert "no bars for NIFTY" in capsys.readouterr().err


def _sweep(tmp_path: Path, **over: object) -> int:
    """Persist the lake + run cmd_sweep with sensible defaults, overridable per test."""
    lake_root, catalog_path = _persist(tmp_path)
    kwargs: dict[str, object] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbol": _SYMBOL,
        "interval": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "direction": "BUY",
        "k": 2,
        "conditions": "0,1,2",
        "stop_loss_paisa": None,
        "take_profit_paisa": None,
        "top": 10,
        "min_hits": None,
        "sort_by": "composite",
        "validate_oos": False,
        "oos_only": False,
    }
    kwargs.update(over)
    return cmd_sweep(**kwargs)  # type: ignore[arg-type]


def test_sweep_oos_only_filters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Single-day lake → no gate survivors → the filter runs over an empty board.
    rc = _sweep(tmp_path, oos_only=True)
    assert rc == 0
    assert "oos-only=0/0 held" in capsys.readouterr().out


def test_sweep_invalid_k_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # k > number of conditions → sweep_report raises → clear message + non-zero exit.
    rc = _sweep(tmp_path, conditions="0,1", k=9)
    assert rc == 1
    assert "sweep error" in capsys.readouterr().err

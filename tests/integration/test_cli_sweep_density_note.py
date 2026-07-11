"""Integration — the sweep-all WARN-only session-density NOTE (DEC-SESSION-DENSITY-CENSUS-001).

``cmd_sweep_all`` pre-loads every ``(symbol, interval)`` slice, so the census's
retro-lake arm runs over the SAME bars the sweep is about to consume (zero
extra lake I/O): an interior low-density session is NAMED on stdout as a
WARNING and the sweep PROCEEDS (EXIT_OK — never a refusal, no escape flag; the
``one_day_trade_law_note`` posture). A clean lake prints nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.cli import EXIT_OK, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over the tiny grid."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _session_bars(d: date) -> list[CandleBar]:
    """A complete 375-bar session on the regular trading day *d*."""
    out: list[CandleBar] = []
    for i in range(375):
        hh, mm = divmod(9 * 60 + 15 + i, 60)
        ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i % 50) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _seed(tmp_path: Path, *, holey: bool) -> tuple[Path, Path]:
    """Seed a NIFTY 1minute lake: Tue + Thu full; Wed present iff not *holey*."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    days = [date(2024, 1, 2), date(2024, 1, 4)]
    if not holey:
        days.insert(1, date(2024, 1, 3))
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        for d in days:
            sink.append(
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                interval="1minute",
                bars=_session_bars(d),
            )
    return lake_root, catalog_path


def _kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "intervals": "1minute",
        "start": "2024-01-02",
        "end": "2024-01-05",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def test_interior_hole_prints_the_note_and_the_sweep_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE pin: a fully-missing interior session (Wed 2024-01-03 — under
    interior-gap's tolerance-5, invisible to every pre-census detector) is
    NAMED on stdout with its shape, and the launch is NEVER refused."""
    lake_root, catalog_path = _seed(tmp_path, holey=True)
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path))
    out = capsys.readouterr().out
    assert rc == EXIT_OK  # WARN-only — the sweep ran to completion
    assert "low-density session(s)" in out
    assert "DEC-SESSION-DENSITY-CENSUS-001" in out
    assert "NIFTY/1minute 2024-01-03: 0/375 bars" in out
    assert "missing_session" in out
    assert "re-pull" in out


def test_clean_lake_prints_no_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A dense lake's launch output is byte-free of the census note."""
    lake_root, catalog_path = _seed(tmp_path, holey=False)
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path))
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "low-density" not in out

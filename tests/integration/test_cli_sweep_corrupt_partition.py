"""R2N-02 CLI integration: a corrupt registered partition REFUSES the sweep launch.

DEC-CATALOG-READ-INTEGRITY-001, MIGRATION.md #1642. The engine's load-bearing
input mouth (``Catalog.get_bars``) NEVER silently skips a corrupt partition — a
dropped file would hole the ``data_snapshot_hash`` inputs and silently change run
identity (Golden Rule #4). ``cmd_sweep_all`` maps the typed ``CatalogError`` to a
clean ``[brutex] REFUSED: …`` on stderr + ``EXIT_USER_ERROR``, never a traceback.

The partition is registered CLEAN first (reconcile is upsert-only, so the stale
catalog row survives the later corruption), then the parquet is overwritten with
garbage so the preload's ``ReplayFeed`` → ``get_bars`` read raises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.cli import EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCH, _SEG, _SYM, _INT = "NSE", "INDEX", "NIFTY", "1minute"


def _trending_bars(n_days: int = 6, per_day: int = 60) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2024, 1, 2, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return bars


def _build_lake(root: Path) -> tuple[Path, Path]:
    lake_root, catalog_path = root / "lake", root / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_EXCH, segment=_SEG, symbol=_SYM, interval=_INT, bars=_trending_bars()
        )
    return lake_root, catalog_path


def _sweep_kwargs(lake_root: Path, catalog_path: Path, root: Path) -> dict[str, object]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": root / "sweeps",
        "exchange": _EXCH,
        "segment": _SEG,
        "symbols": _SYM,
        "intervals": _INT,
        "start": "2024-01-02",
        "end": "2024-01-10",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2,3,4",
        "sl": "80,none",
        "tp": "50",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def test_corrupt_registered_partition_refuses_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _build_lake(tmp_path)
    part = build_partition_path(
        lake_root=lake_root,
        exchange=_EXCH,
        segment=_SEG,
        symbol=_SYM,
        interval=_INT,
        year=2024,
        month=1,
    )
    assert part.exists()
    part.write_bytes(b"CORRUPT\x00\x01" * 40)

    rc = cmd_sweep_all(**_sweep_kwargs(lake_root, catalog_path, tmp_path))  # type: ignore[arg-type]
    assert rc == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "UNREADABLE" in err
    assert str(part) in err
    # A clean user-facing refusal, never a raw traceback.
    assert "Traceback" not in err

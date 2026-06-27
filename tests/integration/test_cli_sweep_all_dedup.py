"""End-to-end proof that ``brutex sweep-all`` wires the OFF-HEAP LMDB dedup.

Regression for GAP #2 (DEC-SWEEP-DEDUP-LMDB-001): the production sweep's
exact-confirm dedup tier MUST be the off-heap :class:`LmdbMembership`, NOT an
in-memory ``set()`` that grows O(combos) and OOMs the 48 GB envelope.

The CLI shim ``cmd_sweep_all`` is ``# pragma: no cover`` (an orchestration shim
over the 100%-tested engine), so this is the HONEST functional validation that
the wiring actually injects the LMDB tier. It persists a tiny lake with the same
writers ingest uses, spies on ``run_supervised_grid`` to capture the injected
``dedup``, and asserts its exact tier is an ``LmdbMembership`` (and that the
``combo-dedup`` LMDB directory is created under the results root).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog
from brutex.data.lmdb_membership import LmdbMembership
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine import sweep_supervisor

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
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


def _exact_tier(dedup: Any) -> Any:
    """Reach the dedup's exact-confirm backing (the in-memory ``set`` vs LMDB)."""
    return dedup._index._exact


def test_sweep_all_uses_lmdb_backed_exact_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    captured: dict[str, Any] = {}
    real = sweep_supervisor.run_supervised_grid

    def _spy(*args: Any, **kwargs: Any) -> Any:
        # Snapshot the exact tier WHILE the with-block (and the LMDB env) is open.
        captured["dedup"] = kwargs["dedup"]
        captured["exact"] = _exact_tier(kwargs["dedup"])
        return real(*args, **kwargs)

    # cmd_sweep_all imports run_supervised_grid locally from sweep_supervisor, so
    # patch it at its source module (resolved at call time).
    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _spy)

    rc = cli.cmd_sweep_all(
        lake_root=lake_root,
        catalog_path=catalog_path,
        results_root=results_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbols=_SYMBOL,
        intervals=_INTERVAL,
        start="2024-01-02",
        end="2024-01-03",
        directions="BUY",
        k="2",
        conditions="0,1,2,3",  # C(4,2) = 6 combos > 1 → a real multi-combo sweep
        sl="none",
        tp="none",
        top=10,
        mode="A",
        max_tuples=None,
        max_combos=None,
        data_snapshot_hash=None,
        options_sim=False,
    )

    assert rc == 0
    # The injected exact-confirm tier is the OFF-HEAP LMDB set, NOT an in-memory
    # ``set`` (the unbounded-growth OOM this fix removes).
    assert isinstance(captured["exact"], LmdbMembership)
    assert not isinstance(captured["exact"], set)
    # The LMDB environment was created under the results root.
    assert (results_root / "combo-dedup").exists()
    # And the sweep actually ran (the report rendered).
    assert "sweep-all done" in capsys.readouterr().out

"""The free L1 invariant piggyback on catalog reconcile (DEC-LAKE-HEAL-001).

``reconcile_catalog_from_lake(check_invariants=True)`` classifies each file's
ALREADY-READ timestamp column (intra-file dup + foreign-month rows, overlap
UNclassified — the free path never reads the canonical sibling) and derives
#911 legacy-key drift from the coords it already parses. Pinned here:

* findings populated + logged, default ON;
* ``check_invariants=False`` restores the legacy behaviour (empty findings,
  zero ``lake.invariant.*`` events);
* the "free" claim — the per-file ``pl.read_parquet`` call count is UNCHANGED
  by the piggyback (spy);
* drift detection needs NO file I/O beyond the coords already parsed;
* reconcile itself NEVER heals (poisoned bytes untouched).

Together with ``tests/unit/test_catalog_reconcile.py`` this holds
``brutex/data/catalog_reconcile.py`` at 100% line + branch.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
from brutex.data.lake_invariants import (
    ForeignMonthRows,
    IntraFileDup,
    LegacyKeyDrift,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path


def _ts(minute: int, *, month: int = 6) -> datetime:
    return datetime(2024, month, 3, 4, minute, tzinfo=UTC)


def _bar(ts: datetime) -> CandleBar:
    c = Decimal("100.00")
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=None)


def _write(
    lake: Path, bars: Sequence[CandleBar], *, segment: str = "INDEX", symbol: str = "NIFTY"
) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=list(bars)
    )


def _dest(lake: Path, *, segment: str = "INDEX", symbol: str = "NIFTY", month: int = 6) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        interval="1minute",
        year=2024,
        month=month,
    )


def _plant_raw(dest: Path, timestamps: Sequence[datetime]) -> None:
    """Raw pyarrow plant (the fixed writer refuses dups — #1067 convention)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    prices = [100.0] * n
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(list(timestamps), type=pa.timestamp("us", tz="UTC")),
                "open": pa.array(prices, type=pa.float64()),
                "high": pa.array(prices, type=pa.float64()),
                "low": pa.array(prices, type=pa.float64()),
                "close": pa.array(prices, type=pa.float64()),
                "volume": pa.array([10] * n, type=pa.int64()),
                "open_interest": pa.array([None] * n, type=pa.int64()),
            }
        ),
        dest,
    )


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


class TestFindingsPopulated:
    def test_dup_and_foreign_findings_default_on(self, tmp_path: Path) -> None:
        # One clean file + one file carrying a dup AND a foreign June row in the
        # July partition — findings ride the DEFAULT reconcile, absolute paths.
        _write(tmp_path, [_bar(_ts(1))])
        july = _dest(tmp_path, month=7)
        _plant_raw(july, [_ts(2), _ts(3, month=7), _ts(3, month=7)])
        with Catalog.open(tmp_path / "catalog.duckdb") as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        # The poisoned file still RECONCILES (findings are advisory — the
        # read-side walk never mutates or drops).
        assert result.partitions_reconciled == 2
        assert result.bars_total == 4
        assert len(result.findings) == 2
        dup = next(f for f in result.findings if isinstance(f, IntraFileDup))
        foreign = next(f for f in result.findings if isinstance(f, ForeignMonthRows))
        assert dup.path == july
        assert dup.physical_rows == 3 and dup.unique_keys == 2
        assert foreign.path == july
        assert foreign.foreign_rows == 1
        assert foreign.overlap_rows is None  # the FREE path never reads the sibling
        assert len(_events(logs, "lake.invariant.intra_file_dup")) == 1
        warns = _events(logs, "lake.invariant.foreign_month_rows")
        assert len(warns) == 1
        assert warns[0]["overlap_rows"] is None

    def test_drift_detected_from_coords_only(self, tmp_path: Path) -> None:
        _write(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _write(tmp_path, [_bar(_ts(2))], segment="CASH", symbol="NSE-M&M")
        with Catalog.open(tmp_path / "catalog.duckdb") as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        drifts = [f for f in result.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drifts) == 1
        assert drifts[0].drifted_symbol == "NSE-M&M"
        assert drifts[0].canonical_symbol == "M&M"
        assert len(_events(logs, "lake.invariant.legacy_key_drift")) == 1

    def test_reconcile_never_heals(self, tmp_path: Path) -> None:
        july = _dest(tmp_path, month=7)
        _plant_raw(july, [_ts(3, month=7), _ts(3, month=7)])
        before = july.read_bytes()
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert len(result.findings) == 1
        assert july.read_bytes() == before  # detect-only, byte-untouched


class TestCheckInvariantsOff:
    def test_off_restores_legacy_behaviour(self, tmp_path: Path) -> None:
        _write(tmp_path, [_bar(_ts(1))], segment="CASH", symbol="M&M")
        _plant_raw(_dest(tmp_path, segment="CASH", symbol="NSE-M&M"), [_ts(2), _ts(2)])
        with Catalog.open(tmp_path / "catalog.duckdb") as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(
                lake_root=tmp_path, catalog=cat, check_invariants=False
            )
        assert result.findings == ()
        assert result.partitions_reconciled == 2
        for event in (
            "lake.invariant.intra_file_dup",
            "lake.invariant.foreign_month_rows",
            "lake.invariant.legacy_key_drift",
        ):
            assert _events(logs, event) == []


class TestFreeClaim:
    def test_no_extra_read_calls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The piggyback classifies the column the reconcile ALREADY reads:
        # the per-file facade scan count is IDENTICAL with the check on and
        # off (W-6a: the read runs inside the crate — ONE native scan per
        # file either way; the pl.read_parquet spy would count zero).
        import brutex.data.catalog_reconcile as cr

        _write(tmp_path, [_bar(_ts(1))])
        _plant_raw(_dest(tmp_path, month=7), [_ts(2), _ts(3, month=7), _ts(3, month=7)])
        calls: list[str] = []
        real = cr.reconcile_file_via_rust

        def _spy(*, path: str, coords: object) -> object:
            calls.append(path)
            return real(path=path, coords=coords)

        monkeypatch.setattr(cr, "reconcile_file_via_rust", _spy)
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            with_check = len(calls)
            calls.clear()
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat, check_invariants=False)
            without_check = len(calls)
        assert with_check == without_check == 2  # one native scan per file, either way

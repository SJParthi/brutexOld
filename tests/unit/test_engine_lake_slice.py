"""Unit tests for the COMMON lake-slice read (``brutex.engine.lake_slice``).

100% line + branch coverage of :func:`~brutex.engine.lake_slice.load_lake_slice`
— the ONE reconcile → heal → feed → dedup slice-read law extracted from
``cascade_wire.load_bars_for_manifest`` (DEC-VIX-STAMP-WIRE-001). Driven over a
REAL tmp lake (raw pyarrow-planted partitions + a real DuckDB catalog +
``ReplayFeed``), covering:

* the clean-lake fast path (no findings → no heal, no quarantine, no audit);
* the reconcile-before-read contract (a fresh empty catalog still serves the
  on-disk bars — reading without reconciling would return nothing);
* the ``[start, end]`` window bound;
* the empty-lake honest ``[]`` (never an error, never a fabricated bar);
* the poisoned-duplicate L1 heal branch (DEC-LAKE-HEAL-001): keep-FIRST winners
  returned, on-disk file healed, quarantine copy + hash-chained audit line.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.data.partition import build_partition_path

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="Catalog needs duckdb")

from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.engine.lake_slice import load_lake_slice


def _ts(minute: int) -> datetime:
    """A UTC 1-minute instant inside the June-2024 partition (Mon 3 Jun, IST session)."""
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _plant_partition(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Write a RAW pyarrow partition (bypasses the writer so dups can be planted)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price, _ in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([vol for _, _, vol in rows], type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _dest(lake: Path) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        year=2024,
        month=6,
    )


def _load(
    lake: Path,
    *,
    start: datetime,
    end: datetime,
) -> list[CandleBar]:
    with Catalog.open(lake / "catalog.duckdb") as catalog:
        return load_lake_slice(
            lake_root=lake,
            catalog=catalog,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            start=start,
            end=end,
            dedup_source="test:NIFTY/1minute",
        )


class TestCleanSlice:
    def test_reads_bars_chronologically_from_fresh_catalog(self, tmp_path: Path) -> None:
        """Reconcile-before-read: a brand-new empty catalog still serves the lake."""
        _plant_partition(
            _dest(tmp_path), [(_ts(16), 100.0, 10), (_ts(17), 101.0, 11), (_ts(18), 102.0, 12)]
        )
        bars = _load(tmp_path, start=_ts(0), end=_ts(59))
        assert [bar.timestamp for bar in bars] == [_ts(16), _ts(17), _ts(18)]
        assert [str(bar.close) for bar in bars] == ["100.0", "101.0", "102.0"]

    def test_clean_lake_never_heals(self, tmp_path: Path) -> None:
        """The no-findings branch: no quarantine copy, no heal audit file."""
        _plant_partition(_dest(tmp_path), [(_ts(16), 100.0, 10)])
        _load(tmp_path, start=_ts(0), end=_ts(59))
        assert not (tmp_path / ".quarantine").exists()
        assert not (tmp_path / "audit" / "lake-heal.jsonl").exists()

    def test_window_bounds_the_read(self, tmp_path: Path) -> None:
        _plant_partition(
            _dest(tmp_path), [(_ts(16), 100.0, 10), (_ts(17), 101.0, 11), (_ts(18), 102.0, 12)]
        )
        bars = _load(tmp_path, start=_ts(17), end=_ts(17))
        assert [bar.timestamp for bar in bars] == [_ts(17)]

    def test_empty_lake_returns_empty_list(self, tmp_path: Path) -> None:
        """No partition on disk → honest ``[]``, never an error / fabricated bar."""
        assert _load(tmp_path, start=_ts(0), end=_ts(59)) == []


class TestPoisonedSliceHeals:
    def test_duplicate_timestamp_heals_keep_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-LAKE-HEAL-001 L1: the findings branch heals BEFORE the feed reads.

        The planted partition carries a verbatim duplicate timestamp; the slice
        read must return the keep-FIRST winner, heal the on-disk file to unique
        timestamps, quarantine the ORIGINAL bytes, and append one hash-chained
        audit line.
        """
        monkeypatch.delenv("BRUTEX_LAKE_HEAL", raising=False)  # default = auto heal
        dest = _dest(tmp_path)
        _plant_partition(
            dest,
            [
                (_ts(16), 100.0, 10),  # keep-first winner for _ts(16)
                (_ts(16), 999.0, 99),  # the poisoned duplicate (later physical row loses)
                (_ts(17), 101.0, 11),
            ],
        )
        original_bytes = dest.read_bytes()

        bars = _load(tmp_path, start=_ts(0), end=_ts(59))

        assert [bar.timestamp for bar in bars] == [_ts(16), _ts(17)]
        assert str(bars[0].close) == "100.0"

        import polars as pl

        healed = pl.read_parquet(dest)
        stored_ts = healed.get_column("timestamp").to_list()
        assert stored_ts == sorted(set(stored_ts)) == [_ts(16), _ts(17)]

        quarantined = list((tmp_path / ".quarantine").rglob("*.parquet"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes

        audit = tmp_path / "audit" / "lake-heal.jsonl"
        assert audit.exists()
        assert "LAKE_HEAL_DEDUP" in audit.read_text(encoding="utf-8")

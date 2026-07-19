"""Unit + property coverage for :mod:`brutex.data.catalog_reconcile`.

Drives :func:`reconcile_catalog_from_lake` + its helpers to **100% line +
branch** over a real on-disk Parquet + DuckDB lake (DEC-CATALOG-RECONCILE-WIRE-001).
The reconciler is the read-side repair that lets ``brutex sweep-all`` see bars in
a lake whose partitions were written but never registered (every production
ingest sink historically passed ``catalog=None``).

Covered branches:

* ``_partition_files``: missing ``bars/`` dir → ``[]``; present → globbed; each
  coordinate filter both set and ``None`` (the ``*`` widening).
* ``reconcile_catalog_from_lake``: a valid partition (upsert + counts), an
  unparseable partition path (``PartitionError`` skip), a zero-row Parquet (skip),
  idempotent re-reconcile, and the ``raw_hash`` == ``sha256(file bytes)`` contract.
* ``_as_utc``: tz-aware passthrough (``astimezone``) and tz-naive stamping
  (``replace``).
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import (
    ReconcileResult,
    _as_utc,
    _partition_files,
    reconcile_catalog_from_lake,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path

pytest.importorskip("duckdb")
pytest.importorskip("polars")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _write_partition(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: int = 2021,
    month: int = 1,
    n: int = 5,
    open_price: float = 100.0,
) -> Path:
    """Write *n* bars into one ``(year, month)`` partition; return the file path."""
    writer = ParquetWriter(lake_root=lake)
    bars = [
        CandleBar(
            timestamp=datetime(year, month, 1 + i, 9, 15, tzinfo=UTC),
            open=Decimal(str(open_price + i)),
            high=Decimal(str(open_price + i + 10)),
            low=Decimal(str(open_price + i - 10)),
            close=Decimal(str(open_price + i + 5)),
            volume=100 + i,
            open_interest=500,
        )
        for i in range(n)
    ]
    writer.write(exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars)
    return build_partition_path(
        lake_root=lake,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
    )


def _open(lake: Path) -> Catalog:
    return Catalog.open(lake / "catalog.duckdb")


# ────────────────────────────────────────────────────────────────────────
# _partition_files
# ────────────────────────────────────────────────────────────────────────


class TestPartitionFiles:
    def test_missing_bars_dir_returns_empty(self, tmp_path: Path) -> None:
        assert (
            _partition_files(tmp_path, exchange=None, segment=None, symbol=None, interval=None)
            == []
        )

    def test_lists_all_partitions_when_unfiltered(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        found = _partition_files(tmp_path, exchange=None, segment=None, symbol=None, interval=None)
        assert len(found) == 2
        assert all(p.suffix == ".parquet" for p in found)

    def test_coordinate_filter_narrows_results(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        only_nifty = _partition_files(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
        )
        assert len(only_nifty) == 1
        assert "NIFTY" in str(only_nifty[0]) and "BANKNIFTY" not in str(only_nifty[0])


# ────────────────────────────────────────────────────────────────────────
# reconcile_catalog_from_lake
# ────────────────────────────────────────────────────────────────────────


class TestReconcile:
    def test_registers_partition_and_returns_counts(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, n=5)
        with _open(tmp_path) as cat:
            assert (
                cat.list_partitions(
                    exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
                )
                == []
            )
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result == ReconcileResult(partitions_reconciled=1, bars_total=5, skipped=0)
            parts = cat.list_partitions(
                exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
            )
            assert len(parts) == 1
            assert parts[0].row_count == 5
            assert parts[0].year == 2021 and parts[0].month == 1

    def test_raw_hash_is_sha256_of_file_bytes(self, tmp_path: Path) -> None:
        path = _write_partition(tmp_path, n=3)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with _open(tmp_path) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            entry = cat.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2021,
                month=1,
            )
            assert entry is not None
            assert entry.raw_hash == expected

    def test_first_last_ts_span_the_bars(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, n=4)
        with _open(tmp_path) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            entry = cat.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2021,
                month=1,
            )
            assert entry is not None
            assert entry.first_ts == datetime(2021, 1, 1, 9, 15, tzinfo=UTC)
            assert entry.last_ts == datetime(2021, 1, 4, 9, 15, tzinfo=UTC)

    def test_coordinate_filter_only_reconciles_match(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, symbol="NIFTY")
        _write_partition(tmp_path, symbol="BANKNIFTY")
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(
                lake_root=tmp_path,
                catalog=cat,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
            )
            assert result.partitions_reconciled == 1
            assert (
                cat.list_partitions(
                    exchange="NSE", segment="INDEX", symbol="BANKNIFTY", interval="1minute"
                )
                == []
            )

    def test_is_idempotent(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, n=5)
        with _open(tmp_path) as cat:
            first = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            second = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert first == second
            assert cat.count() == 1

    def test_skips_unparseable_partition_path(self, tmp_path: Path) -> None:
        # A file that glob-matches the six-segment layout but whose exchange
        # segment is invalid → parse_partition_path raises → skipped.
        bad = tmp_path / "bars" / "ZZ" / "INDEX" / "NIFTY" / "1minute" / "2021" / "01.parquet"
        bad.parent.mkdir(parents=True)
        pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(
            bad
        )
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result == ReconcileResult(partitions_reconciled=0, bars_total=0, skipped=1)

    def test_skips_zero_row_partition(self, tmp_path: Path) -> None:
        empty = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2021" / "01.parquet"
        empty.parent.mkdir(parents=True)
        pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(
            empty
        )
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result == ReconcileResult(partitions_reconciled=0, bars_total=0, skipped=1)

    def test_empty_lake_is_a_clean_noop(self, tmp_path: Path) -> None:
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result == ReconcileResult(partitions_reconciled=0, bars_total=0, skipped=0)

    def test_corrupt_partition_is_skipped_others_reconcile(self, tmp_path: Path) -> None:
        """GAP #9: a corrupt Parquet is logged + skipped; every readable partition still reconciles.

        The per-file read runs at the START of every sweep load + F&O window derivation, so
        a single garbage file must NOT crash the whole reconcile — it is counted in
        ``skipped`` (and a ``catalog.reconcile.parquet_skip`` warning emitted), while the
        GOOD partition reconciles normally.
        """
        from structlog.testing import capture_logs

        _write_partition(tmp_path, symbol="NIFTY", n=4)  # one GOOD partition
        # A SECOND file that glob-matches the layout but is GARBAGE bytes (a corrupt /
        # version-incompatible Parquet), so the REAL pl.read_parquet raises a PolarsError.
        corrupt = (
            tmp_path / "bars" / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2021" / "01.parquet"
        )
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a parquet file at all")
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            # The good partition reconciled; the corrupt one was skipped (not a crash).
            assert result == ReconcileResult(partitions_reconciled=1, bars_total=4, skipped=1)
            parts = cat.list_partitions(
                exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
            )
            assert len(parts) == 1
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1
        assert "BANKNIFTY" in skips[0]["path"]

    def test_out_of_range_timestamp_partition_is_skipped_others_reconcile(
        self, tmp_path: Path
    ) -> None:
        """Round-18 fold (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): a hand-planted
        Datetime column holding an out-of-``datetime``-range i64 (2**62 µs)
        made ``timestamps.min()`` raise ``OverflowError`` — NOT a
        ``parquet_read_skip_errors`` member — crashing the WHOLE reconcile
        (which runs at the start of every sweep load). Now skipped + counted
        under the COMMON ``datetime_out_of_range_errors`` fault set, the good
        partition still reconciles."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        from structlog.testing import capture_logs

        _write_partition(tmp_path, symbol="NIFTY", n=4)  # one GOOD partition
        bad = tmp_path / "bars" / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2021" / "01.parquet"
        bad.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array([2**62], type=pa.int64()).cast(
                        pa.timestamp("us", tz="UTC")
                    ),
                }
            ),
            bad,
        )
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            # pre-fix: OverflowError propagated out of the call above
            assert result == ReconcileResult(partitions_reconciled=1, bars_total=4, skipped=1)
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1
        assert "BANKNIFTY" in skips[0]["path"]
        assert "out of range" in str(skips[0]["error"])

    def test_reconciled_partition_is_get_bars_readable(self, tmp_path: Path) -> None:
        # The whole point: after reconcile, get_bars (the sweep read path) returns bars.
        _write_partition(tmp_path, n=6)
        with _open(tmp_path) as cat:
            reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            table = cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                start=datetime(2021, 1, 1, tzinfo=UTC),
                end=datetime(2021, 2, 1, tzinfo=UTC),
            )
            assert table.num_rows == 6


# ────────────────────────────────────────────────────────────────────────
# The Wave-D W-3 seam (BRUTEX_LAKE_WAVED_RUST)
# ────────────────────────────────────────────────────────────────────────


class TestWavedSeam:
    """The flag gate around the per-file scan body (DEC-RUST-LAKE-WAVED-001).

    Wheel-free by design: the flag-ON arm poisons ``sys.modules`` so the
    facade's import guard latches and the ORACLE (the verbatim
    ``_scan_file``) serves — proving the routed branch is
    behaviour-identical without the crate; the real-wheel parity gate is
    ``tests/integration/test_waved_reconcile_differential.py``.
    """

    def test_flag_off_never_calls_the_facade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The never-routes bomb spy: flag OFF (the default) must not touch
        # the routing function at all.
        import brutex.data.catalog_reconcile as cr

        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)

        def _bomb(**kwargs: object) -> object:
            raise AssertionError("reconcile_file_via_rust must not run flag-OFF")

        monkeypatch.setattr(cr, "reconcile_file_via_rust", _bomb)
        _write_partition(tmp_path, n=3)
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert result.partitions_reconciled == 1

    def test_flag_on_without_crate_serves_the_oracle_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag ON + an absent wheel: the facade latches crate_unavailable and
        # every per-file call serves the verbatim Python body — the result is
        # byte-identical to the flag-OFF run over the same lake.
        import sys

        from brutex.data import lake_rust_waved as lrw

        _write_partition(tmp_path, n=4)
        with _open(tmp_path) as cat:
            baseline = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # ImportError → latch
        lrw.reset_lake_rust_waved_for_tests()
        try:
            with _open(tmp_path) as cat:
                routed = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        finally:
            lrw.reset_lake_rust_waved_for_tests()
        assert routed == baseline
        assert routed.partitions_reconciled == 1 and routed.bars_total == 4


# ────────────────────────────────────────────────────────────────────────
# _as_utc
# ────────────────────────────────────────────────────────────────────────


class TestAsUtc:
    def test_tz_aware_passthrough(self) -> None:
        ts = datetime(2021, 1, 1, 9, 15, tzinfo=UTC)
        assert _as_utc(ts) == ts

    def test_tz_aware_non_utc_is_converted(self) -> None:
        from datetime import timedelta, timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        ts = datetime(2021, 1, 1, 14, 45, tzinfo=ist)
        assert _as_utc(ts) == datetime(2021, 1, 1, 9, 15, tzinfo=UTC)

    def test_tz_naive_is_stamped_utc(self) -> None:
        naive = datetime(2021, 1, 1, 9, 15)
        assert _as_utc(naive) == datetime(2021, 1, 1, 9, 15, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────
# Property: any set of valid partitions reconciles fully + idempotently
# ────────────────────────────────────────────────────────────────────────


class TestProperty:
    @pytest.mark.parametrize(
        "symbols",
        [("NIFTY",), ("NIFTY", "BANKNIFTY"), ("NIFTY", "BANKNIFTY", "SENSEX")],
    )
    def test_all_written_partitions_are_registered(
        self, tmp_path: Path, symbols: tuple[str, ...]
    ) -> None:
        total = 0
        for sym in symbols:
            ex = "BSE" if sym == "SENSEX" else "NSE"
            _write_partition(tmp_path, exchange=ex, symbol=sym, n=3)
            total += 1
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result.partitions_reconciled == total
            assert result.bars_total == total * 3

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
  unparseable partition path (``PartitionError`` skip), a PRESENT 0-row Parquet
  (skip + flagged on ``empty_partitions``, R2N-06), idempotent re-reconcile, and
  the ``raw_hash`` == ``sha256(file bytes)`` contract.
* ``_as_utc``: tz-aware passthrough (``astimezone``) and tz-naive stamping
  (``replace``).
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
import hashlib
from collections.abc import Callable
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

    def test_present_zero_row_partition_is_skipped_and_flagged_empty(self, tmp_path: Path) -> None:
        """DEC-LAKE-STATS-MANIFEST-001 residual R2N-06: a PRESENT, readable,
        EXACTLY-0-row partition is counted a benign ``skipped`` AND recorded
        DISTINCTLY on ``empty_partitions`` + one ``catalog.reconcile.empty_partition``
        WARN — no BruteX writer emits a valid 0-row parquet, so it is externally-forged
        and the sweep launch must be able to REFUSE it (else the whole month silently
        drops). Fails-before: the file was a benign zero-row skip with ``empty_partitions``
        empty, so the launch swept the smaller lake with a data_snapshot_hash identical
        to the drop.
        """
        from structlog.testing import capture_logs

        empty = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2021" / "01.parquet"
        empty.parent.mkdir(parents=True)
        pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(
            empty
        )
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
            assert result == ReconcileResult(
                partitions_reconciled=0,
                bars_total=0,
                skipped=1,
                empty_partitions=(str(empty),),
            )
        events = [e for e in logs if e["event"] == "catalog.reconcile.empty_partition"]
        assert len(events) == 1
        assert events[0]["path"] == str(empty)

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
            # DEC-SWEEP-RECONCILE-CORRUPT-REFUSE-001 (#1644): the corrupt file's
            # absolute path is also recorded DISTINCTLY on ``unreadable`` (the
            # sweep-launch refusal signal), while ``skipped`` still counts it.
            assert result == ReconcileResult(
                partitions_reconciled=1,
                bars_total=4,
                skipped=1,
                unreadable=(str(corrupt),),
            )
            parts = cat.list_partitions(
                exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
            )
            assert len(parts) == 1
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1
        assert "BANKNIFTY" in skips[0]["path"]

    def test_vanished_file_is_benign_skipped_not_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1644 round 1 M1, NARROWED by round 2 M3: a ``FileNotFoundError``
        (ENOENT — the file VANISHES between the glob listing and the read, a
        concurrent heal/fetch) is data ABSENCE, not data LOSS — counted in
        ``skipped`` (WARN kept) but NEVER on ``unreadable``, so the escape-less
        sweep-launch refusal cannot be tripped by a transient FS glitch.
        ENOENT is now the ONLY benign OSError subclass (round 2 M3 reclassified
        every OTHER OSError as present-but-unreadable → ``unreadable``).

        Fails-before: the pre-fix combined ``parquet_read_skip_errors()`` arm
        recorded the vanished file on ``unreadable`` → a launch REFUSAL.
        """
        import errno
        from types import SimpleNamespace

        import brutex.data.catalog_reconcile as cr
        from structlog.testing import capture_logs

        _write_partition(tmp_path, symbol="NIFTY", n=4)  # a GOOD partition
        target = _write_partition(tmp_path, symbol="BANKNIFTY", n=3)
        real_read = pl.read_parquet

        def _vanishing_read(path: object, *args: object, **kwargs: object) -> object:
            if Path(str(path)) == target:
                # The file genuinely vanishes between listing and read.
                target.unlink(missing_ok=True)
                raise FileNotFoundError(errno.ENOENT, "vanished mid-reconcile", str(target))
            return real_read(path, *args, **kwargs)  # type: ignore[arg-type]

        # Scope the shim to the module-level `pl` name (never the shared polars
        # module object) so only the reconcile's reads are intercepted.
        monkeypatch.setattr(
            cr, "pl", SimpleNamespace(read_parquet=_vanishing_read, exceptions=pl.exceptions)
        )
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = cr.reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert result.partitions_reconciled == 1  # the good partition still lands
        assert result.skipped == 1
        assert result.unreadable == ()  # absent != corrupt — never a refusal signal
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1
        assert "BANKNIFTY" in skips[0]["path"]

    def test_unreadable_names_only_the_corrupt_file_among_benign_skips(
        self, tmp_path: Path
    ) -> None:
        """DEC-SWEEP-RECONCILE-CORRUPT-REFUSE-001 (#1644): the skip classes
        disambiguate — a CORRUPT/unreadable partition lands on ``unreadable``, while
        a benign unparseable-path skip AND a benign zero-row skip do NOT (data
        ABSENCE is never data LOSS — the false-refuse guard). A clean partition
        alongside them still reconciles; ``unreadable`` names ONLY the corrupt file.
        (Round 3 M3: ``unreadable`` is populated by the PolarsError arm AND the
        non-ENOENT OSError arm — the directory/permission/EIO tests below — this
        test pins that the two BENIGN classes never reach either arm.)
        """
        _write_partition(tmp_path, symbol="NIFTY", n=3)  # a GOOD partition
        # (a) benign: an unparseable partition PATH (invalid exchange segment).
        unparseable = (
            tmp_path / "bars" / "ZZ" / "INDEX" / "NIFTY" / "1minute" / "2021" / "01.parquet"
        )
        unparseable.parent.mkdir(parents=True)
        pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(
            unparseable
        )
        # (b) benign: a zero-row (empty) partition.
        empty = tmp_path / "bars" / "NSE" / "INDEX" / "SENSEXX" / "1minute" / "2021" / "01.parquet"
        empty.parent.mkdir(parents=True)
        pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(
            empty
        )
        # (c) CORRUPT: garbage bytes → the only ``unreadable`` entry.
        corrupt = (
            tmp_path / "bars" / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2021" / "01.parquet"
        )
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a parquet file at all")
        with _open(tmp_path) as cat:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert result.partitions_reconciled == 1
        assert result.skipped == 3  # unparseable + zero-row + corrupt
        assert result.unreadable == (str(corrupt),)  # ONLY the corrupt file

    def test_data_bearing_directory_at_month_path_is_unreadable(self, tmp_path: Path) -> None:
        """#1644 review round 2 M3 — THE refuter repro, REAL filesystem: a
        DIRECTORY named ``<mm>.parquet/`` holding a real parquet part (the #1558
        FIX-ROUND-8 dataset-directory shape) is PRESENT data the sweep would
        silently skip. ``pl.read_parquet`` reads the directory's rows fine, then
        ``path.read_bytes()`` raises ``IsADirectoryError`` — a non-ENOENT
        OSError — so it must land on ``unreadable`` and REFUSE the launch.

        Fails-before: round 1's "all OSError → benign" carve-out counted it a
        benign ``skipped`` → the sweep launched over the silently-holed month
        (PDH/PDL/pivot/Fib anchors poisoned, the holed series hashed canonical).
        """
        from structlog.testing import capture_logs

        _write_partition(tmp_path, symbol="NIFTY", n=4)  # a GOOD partition
        dir_shard = (
            tmp_path / "bars" / "NSE" / "INDEX" / "BANKNIFTY" / "1minute" / "2021" / "01.parquet"
        )
        dir_shard.mkdir(parents=True)
        # REAL data inside the directory — polars reads it, read_bytes cannot.
        pl.DataFrame(
            {
                "timestamp": pl.Series(
                    [datetime(2021, 1, 4, 9, 15, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
                )
            }
        ).write_parquet(dir_shard / "part-0.parquet")
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert result.partitions_reconciled == 1  # the good partition still lands
        assert result.skipped == 1
        assert result.unreadable == (str(dir_shard),)  # present != absent — REFUSE
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1
        assert "BANKNIFTY" in skips[0]["path"]

    @pytest.mark.parametrize(
        ("exc_factory", "label"),
        [
            (
                lambda p: PermissionError(errno.EACCES, "permission denied", str(p)),
                "permission",
            ),
            (lambda p: OSError(errno.EIO, "I/O error", str(p)), "eio"),
        ],
    )
    def test_permission_and_eio_faults_are_unreadable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc_factory: Callable[[Path], OSError],
        label: str,
    ) -> None:
        """#1644 review round 2 M3: a chmod-000 / wrong-owner file
        (``PermissionError``) and an ``EIO`` device fault are PRESENT data the
        process cannot read — data LOSS, not absence — so each lands on
        ``unreadable`` and REFUSES the launch. (Monkeypatched at the module's
        ``pl`` seam — the vanishing-test pattern — because this suite runs as
        root, where a real chmod-000 file still reads.)

        Fails-before: round 1's "all OSError → benign" arm counted BOTH a
        benign ``skipped`` — a silent-partial sweep launch.
        """
        from types import SimpleNamespace

        import brutex.data.catalog_reconcile as cr
        from structlog.testing import capture_logs

        _write_partition(tmp_path, symbol="NIFTY", n=4)  # a GOOD partition
        target = _write_partition(tmp_path, symbol="BANKNIFTY", n=3)
        real_read = pl.read_parquet

        def _faulting_read(path: object, *args: object, **kwargs: object) -> object:
            if Path(str(path)) == target:
                raise exc_factory(target)
            return real_read(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            cr, "pl", SimpleNamespace(read_parquet=_faulting_read, exceptions=pl.exceptions)
        )
        with _open(tmp_path) as cat, capture_logs() as logs:
            result = cr.reconcile_catalog_from_lake(lake_root=tmp_path, catalog=cat)
        assert result.partitions_reconciled == 1, label
        assert result.skipped == 1
        assert result.unreadable == (str(target),)  # present-but-unreadable — REFUSE
        skips = [e for e in logs if e["event"] == "catalog.reconcile.parquet_skip"]
        assert len(skips) == 1

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

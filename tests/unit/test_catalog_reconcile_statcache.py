"""Reconcile stat-cache — value-safety + the opt-in shared-caller guard.

DEC-SWEEP-RECONCILE-STATCACHE-001. The ``brutex sweep-all`` launcher passes
``reconcile_catalog_from_lake(..., stat_cache=True)`` so a WARM (byte-unchanged)
launch skips the redundant full-bytes ``sha256`` + timestamp re-read of every
partition on an exact ``(mtime_ns, size, ino)`` token match. Pinned here:

* **snapshot-hash equality (the Golden-Rule-#4 proof)** — the launch
  ``data_snapshot_hash`` is byte-identical with the stat-cache OFF, ON-cold, and
  ON-warm: the skip never changes which bars load or their values;
* **warm launch does ZERO parquet reads + ZERO ``sha256``** (read-count spy);
* **cache invalidation** — a changed partition (new mtime/size/ino) recomputes;
* **NULL back-compat** — a row with unstamped (NULL) stat columns is a MISS,
  recomputed + backfilled, so the NEXT launch is warm;
* **TOCTOU** — a partition that vanishes between the glob and the ``os.stat`` is
  skipped gracefully, never a crash;
* **best-effort backfill** — a token-write failure is logged + swallowed (never
  fails the launch) and leaves the file a MISS next launch;
* **shared-caller non-regression** — the default ``stat_cache=False`` path (the
  cascade-wire L1 heal consumer + the ``lake audit-unique`` census) STILL
  detects a byte-unchanged-but-poisoned partition across launches.

Together with ``test_catalog_statcache.py`` these hold
``brutex/data/catalog_reconcile.py`` at 100% line + branch.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data import catalog_reconcile
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import ReconcileResult, reconcile_catalog_from_lake
from brutex.data.errors import CatalogError
from brutex.data.lake_invariants import IntraFileDup
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_START = datetime(2020, 1, 1, tzinfo=UTC)
_END = datetime(2021, 12, 31, 23, 59, 59, tzinfo=UTC)
_START_ISO = "2020-01-01T00:00:00"
_END_ISO = "2021-12-31T23:59:59"


def _write_month(
    lake: Path,
    *,
    interval: str,
    year: int,
    month: int,
    n: int,
    base: float = 100.0,
) -> Path:
    """Write *n* one-per-day bars into one ``(year, month)`` partition."""
    writer = ParquetWriter(lake_root=lake)
    bars = [
        CandleBar(
            timestamp=datetime(year, month, 1 + i, 9, 15, tzinfo=UTC),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + i + 10)),
            low=Decimal(str(base + i - 10)),
            close=Decimal(str(base + i + 5)),
            volume=100 + i,
            open_interest=None,
        )
        for i in range(n)
    ]
    writer.write(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval=interval, bars=bars)
    return build_partition_path(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=interval,
        year=year,
        month=month,
    )


def _build_fixture(lake: Path) -> None:
    """A 3-partition NIFTY lake: 1minute over Jan+Feb 2021, 5minute over Jan 2021."""
    _write_month(lake, interval="1minute", year=2021, month=1, n=5)
    _write_month(lake, interval="1minute", year=2021, month=2, n=4)
    _write_month(lake, interval="5minute", year=2021, month=1, n=3, base=200.0)


_INTERVALS = ("1minute", "5minute")


def _launch(
    catalog_path: Path, lake: Path, *, stat_cache: bool
) -> tuple[str, list[ReconcileResult]]:
    """Mirror the ``cmd_sweep_all`` reconcile+ReplayFeed launch → (digest, results)."""
    from brutex.engine.stage3_wire import data_snapshot_hash
    from brutex.feed.replay import ReplayFeed

    slices: dict[tuple[str, str], list[CandleBar]] = {}
    results: list[ReconcileResult] = []
    with Catalog.open(catalog_path) as cat:
        for interval in _INTERVALS:
            results.append(
                reconcile_catalog_from_lake(
                    lake_root=lake,
                    catalog=cat,
                    exchange=_EXCHANGE,
                    segment=_SEGMENT,
                    symbol=_SYMBOL,
                    interval=interval,
                    stat_cache=stat_cache,
                )
            )
        for interval in _INTERVALS:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbols=[_SYMBOL],
                interval=interval,
                start=_START,
                end=_END,
            )
            slices[(_SYMBOL, interval)] = [event.bar for event in feed]
    digest = data_snapshot_hash(
        exchange=_EXCHANGE, segment=_SEGMENT, start=_START_ISO, end=_END_ISO, slices=slices
    )
    return digest, results


class TestSnapshotHashEquality:
    def test_digest_identical_off_vs_cold_vs_warm(self, tmp_path: Path) -> None:
        # The run-identity teeth: stat_cache never moves data_snapshot_hash.
        lake = tmp_path / "lake"
        lake.mkdir()
        _build_fixture(lake)

        off_digest, _ = _launch(lake / "cat_off.duckdb", lake, stat_cache=False)
        on_cold_digest, cold = _launch(lake / "cat_on.duckdb", lake, stat_cache=True)
        # Re-open the SAME on-catalog → the durable stat tokens make this WARM.
        on_warm_digest, warm = _launch(lake / "cat_on.duckdb", lake, stat_cache=True)

        assert off_digest == on_cold_digest == on_warm_digest
        # Cold ON reconciled all 3 partitions, zero hits …
        assert sum(r.partitions_reconciled for r in cold) == 3
        assert sum(r.stat_cache_hits for r in cold) == 0
        # … warm ON skipped all 3 on stat-only matches, zero reconciles.
        assert sum(r.partitions_reconciled for r in warm) == 0
        assert sum(r.stat_cache_hits for r in warm) == 3


class TestWarmLaunchReadCount:
    def test_warm_reconcile_does_zero_facade_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # W-6a: the per-file read runs INSIDE the crate, so the historical
        # pl.read_parquet / Path.read_bytes spies see nothing — the stat-cache
        # law is pinned one seam up: a warm launch dispatches ZERO per-file
        # facade scans (the cache skip happens BEFORE the crate call).
        from brutex.data import catalog_reconcile as cr

        lake = tmp_path / "lake"
        lake.mkdir()
        _write_month(lake, interval="1minute", year=2021, month=1, n=5)
        _write_month(lake, interval="1minute", year=2021, month=2, n=4)  # 2 partitions

        # W-6a merge note: the per-file body is crate-owned
        # (reconcile_file_via_rust) — spy THAT seam; the #1948-era
        # read_parquet/read_regular_bytes seams no longer exist on this path.
        calls: list[str] = []
        real = cr.reconcile_file_via_rust

        from brutex.data.lake_invariants import PartitionCoords

        def _spy(*, path: str, coords: PartitionCoords | None) -> object:
            calls.append(path)
            return real(path=path, coords=coords)

        monkeypatch.setattr(cr, "reconcile_file_via_rust", _spy)

        with Catalog.open(lake / "catalog.duckdb") as cat:
            cold = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
            cold_scans = len(calls)
            calls.clear()
            warm = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )

        # Cold: one native scan (ts read + full-bytes sha256) per partition.
        assert cold.partitions_reconciled == 2 and cold.stat_cache_hits == 0
        assert cold_scans == 2
        # Warm: all skipped on stat-only matches — ZERO native scans.
        assert warm.partitions_reconciled == 0 and warm.stat_cache_hits == 2
        assert calls == []


class TestCacheInvalidation:
    def test_changed_partition_recomputes_others_stay_warm(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        lake.mkdir()
        jan = _write_month(lake, interval="1minute", year=2021, month=1, n=5)
        _write_month(lake, interval="1minute", year=2021, month=2, n=4)
        with Catalog.open(lake / "catalog.duckdb") as cat:
            # Warm the cache (cold miss, then all-hit).
            reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
            # Append a bar to the Jan partition → new mtime/size/ino (os.replace).
            _write_month(lake, interval="1minute", year=2021, month=1, n=6)
            assert jan.exists()

            changed = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
        # The changed Jan partition recomputed (MISS); Feb stayed a stat-only hit.
        assert changed.partitions_reconciled == 1
        assert changed.stat_cache_hits == 1


class TestNullBackCompat:
    def test_unstamped_row_is_a_miss_then_backfilled(self, tmp_path: Path) -> None:
        # A row written by the default stat_cache=False path (the shared callers)
        # has NULL stat columns — a legacy/unstamped row. A stat_cache=True launch
        # sees NULL → MISS → recompute → backfill, so the NEXT one is warm.
        lake = tmp_path / "lake"
        lake.mkdir()
        _write_month(lake, interval="1minute", year=2021, month=1, n=5)
        with Catalog.open(lake / "catalog.duckdb") as cat:
            # Shared-caller default — writes the row WITHOUT a stat token.
            reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
            )
            assert cat.get_partition_stat_tokens(
                exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute"
            ) == {(2021, 1): None}

            first = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
            second = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
        # NULL token → MISS on the first stat_cache launch (recompute + backfill) …
        assert first.partitions_reconciled == 1 and first.stat_cache_hits == 0
        # … warm on the second.
        assert second.partitions_reconciled == 0 and second.stat_cache_hits == 1


class TestTocTou:
    def test_vanished_partition_is_skipped_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file that exists at glob time but vanishes before the os.stat
        # (TOCTOU) is counted-skipped, never a crash.
        lake = tmp_path / "lake"
        lake.mkdir()
        real = _write_month(lake, interval="1minute", year=2021, month=1, n=5)
        phantom = build_partition_path(
            lake_root=lake,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval="1minute",
            year=2099,
            month=12,
        )  # a valid layout whose file is never written → os.stat raises

        def fake_partition_files(*args: Any, **kwargs: Any) -> list[Path]:
            return [real, phantom]

        monkeypatch.setattr(catalog_reconcile, "_partition_files", fake_partition_files)
        from structlog.testing import capture_logs

        with Catalog.open(lake / "catalog.duckdb") as cat, capture_logs() as logs:
            result = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
        assert result.partitions_reconciled == 1
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "catalog.reconcile.stat_skip"]
        assert len(skips) == 1
        assert "2099" in skips[0]["path"]


class _StatWriteFailingCatalog(Catalog):
    """A Catalog whose stat-token write always fails (best-effort backfill test)."""

    def set_partition_stat(self, **kwargs: Any) -> None:
        raise CatalogError("simulated stat-cache write failure")


class TestBestEffortBackfill:
    def test_stat_write_failure_never_fails_and_leaves_a_miss(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        lake.mkdir()
        _write_month(lake, interval="1minute", year=2021, month=1, n=5)
        from structlog.testing import capture_logs

        with _StatWriteFailingCatalog.open(lake / "catalog.duckdb") as cat:
            with capture_logs() as logs:
                first = reconcile_catalog_from_lake(
                    lake_root=lake,
                    catalog=cat,
                    exchange=_EXCHANGE,
                    segment=_SEGMENT,
                    symbol=_SYMBOL,
                    interval="1minute",
                    stat_cache=True,
                )
            # The partition still reconciled (the upsert succeeded); the token
            # write failed loudly-but-harmlessly.
            assert first.partitions_reconciled == 1 and first.stat_cache_hits == 0
            fails = [e for e in logs if e["event"] == "catalog.reconcile.stat_write_failed"]
            assert len(fails) == 1
            # The token was never written → the NEXT launch is STILL a MISS.
            second = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
            assert second.partitions_reconciled == 1 and second.stat_cache_hits == 0


def _plant_dup(lake: Path, *, year: int, month: int, timestamps: Sequence[datetime]) -> Path:
    """Plant a poisoned partition carrying intra-file duplicate timestamps."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = build_partition_path(
        lake_root=lake,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        year=year,
        month=month,
    )
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
    return dest


class TestSharedCallerNonRegression:
    def test_default_still_detects_poison_across_launches(self, tmp_path: Path) -> None:
        # The cascade-wire L1 heal consumer + `lake audit-unique` census read
        # `ReconcileResult.findings` and pass the DEFAULT stat_cache=False, so a
        # byte-unchanged-but-poisoned partition is STILL detected on every launch
        # (an opt-in stat_cache skip would have suppressed it — the forbidden
        # regression this pins against).
        lake = tmp_path / "lake"
        lake.mkdir()
        ts = datetime(2021, 1, 3, 9, 15, tzinfo=UTC)
        _plant_dup(lake, year=2021, month=1, timestamps=[ts, ts])
        with Catalog.open(lake / "catalog.duckdb") as cat:
            first = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
            )
            # Byte-unchanged re-run (the row now exists) STILL re-detects.
            second = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
            )
        assert any(isinstance(f, IntraFileDup) for f in first.findings)
        assert any(isinstance(f, IntraFileDup) for f in second.findings)

    def test_lake_audit_unique_census_still_exits_nonzero(self, tmp_path: Path) -> None:
        # The `brutex lake audit-unique` census reads scan_lake_invariants (NOT
        # reconcile), so it is structurally immune to the reconcile stat-cache —
        # a poisoned lake exits non-zero regardless.
        from brutex.cli import EXIT_USER_ERROR, cmd_lake_audit_unique

        lake = tmp_path / "lake"
        lake.mkdir()
        ts = datetime(2021, 1, 3, 9, 15, tzinfo=UTC)
        _plant_dup(lake, year=2021, month=1, timestamps=[ts, ts])
        code = cmd_lake_audit_unique(
            lake_root=lake,
            as_json=True,
            exchange=None,
            segment=None,
            symbol=None,
            interval=None,
            no_options=True,
        )
        assert code == EXIT_USER_ERROR

    def test_optin_launcher_would_skip_the_same_poison_when_warm(self, tmp_path: Path) -> None:
        # The contrast that makes the guard load-bearing: with stat_cache=True the
        # WARM re-run skips the byte-unchanged poisoned partition (findings
        # suppressed) — which is exactly why the shared findings-consumers must
        # keep stat_cache=False. The launcher discards findings + heals nothing.
        lake = tmp_path / "lake"
        lake.mkdir()
        ts = datetime(2021, 1, 3, 9, 15, tzinfo=UTC)
        _plant_dup(lake, year=2021, month=1, timestamps=[ts, ts])
        with Catalog.open(lake / "catalog.duckdb") as cat:
            cold = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
            warm = reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol=_SYMBOL,
                interval="1minute",
                stat_cache=True,
            )
        # Cold detects; warm skips (stat-only hit) → findings suppressed.
        assert any(isinstance(f, IntraFileDup) for f in cold.findings)
        assert warm.findings == ()
        assert warm.stat_cache_hits == 1

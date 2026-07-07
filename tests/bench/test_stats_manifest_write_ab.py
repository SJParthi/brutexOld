"""Write-path A/B bench — stats-manifest hook cost ON vs OFF (DEC-LAKE-STATS-MANIFEST-001).

Spec §10 bench (b): the writer hook's added cost, hooks-ON vs kill-switch-OFF,
on BOTH a VIEWED dataset (a shard exists — the only case that pays a shard
flock + ~KB-scale fsync'd rewrite) and an UNVIEWED one (lazy accretion — one
cheap probe: a shard-path key digest + one failed ``os.stat``, probed FIRST;
no shard ever created; review round 2). Two layers:

* **Deterministic teeth (every runner, never wall-clock):**

  1. byte-PARITY — the RENDERED partition bytes are byte-identical with the
     manifest ON (viewed shard, hook firing every write) and OFF (Golden
     Rule #7: the manifest never enters partition bytes);
  2. the anti-storm law — an UNVIEWED lake accretes ZERO shard files across a
     whole multi-dataset write storm (the prebuild-storm structural claim);
  3. the VIEWED shard tracks every rewrite (rows/token move per append), so
     the ON arm genuinely paid the shard rewrite the timed arm measures.

* **Wall-clock A/B (env-gated ``BRUTEX_M4PRO_ABS=1`` — the
  DEC-HARDENING-LEFTOVERS-001 #1149 flaky-bench precedent: shared-GHA GIL
  noise flips cross-arm median comparisons, so the timed body runs only on
  the quiet reference tier):** the median per-append OVERHEAD of the ON arm
  over the OFF arm on a VIEWED dataset must fit inside the LOW end of the
  measured 12-213 ms merge budget (DEC-DEEP-PARTITION-BENCH-001 #946) — the
  hook may never dominate even the CHEAPEST merge. Mitigation ladder if this
  gate ever fails (spec §7): move the shard upsert to just AFTER the
  partition-lock release (stat still captured UNDER the lock) → kill switch.
"""

from __future__ import annotations

import os
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from brutex.data import lake_stats_manifest as lsm
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, WriteResult

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

#: The one bars dataset the viewed-arm tests write into (canonical engine coords).
_COORDS: Final[dict[str, str]] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

#: Env gate for the wall-clock body (the #1149 shared-CI flaky-bench precedent).
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

#: The LOW end of the #946 measured merge budget (ms) — the hook's overhead
#: ceiling: it must never dominate even the cheapest measured merge.
_MERGE_BUDGET_FLOOR_MS: Final[float] = 12.0

#: Appends timed per arm in the gated wall-clock A/B.
_TIMED_APPENDS: Final[int] = 30

skip_unless_m4pro = pytest.mark.skipif(
    os.environ.get(_ENV_M4PRO_ABS, "").strip() != "1",
    reason=(
        f"{_ENV_M4PRO_ABS}=1 not set — the write-path A/B wall-clock body runs "
        "only on the quiet M4-Pro reference tier (the #1149 flaky-bench precedent); "
        "the deterministic parity/anti-storm teeth in this file run everywhere"
    ),
)


def _bar(ts: datetime, *, close: str = "100.00") -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=None)


def _ts(minute_index: int) -> datetime:
    """The *minute_index*-th minute of one UTC month (2021-01), day-rolled."""
    day = 1 + minute_index // 1_000
    return datetime(2021, 1, day, 4, 0, tzinfo=UTC).replace(
        hour=minute_index % 1_000 // 60, minute=minute_index % 60
    )


def _seed_shard(lake: Path, result: WriteResult) -> Path:
    """Make the dataset VIEWED: seed its shard from one real ``WriteResult``."""
    stat = os.stat(result.path)
    row = lsm.StatsFileRow(
        rel=f"{result.path.parent.name}/{result.path.name}",
        rows=result.row_count,
        min_ts=result.first_ts,
        max_ts=result.last_ts,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        raw_hash=result.raw_hash,
    )
    assert lsm.seed_dataset_stats(lake, kind=lsm.KIND_BARS, key=_COORDS, files=[row])
    return lsm.stats_shard_path(lake, lsm.KIND_BARS, _COORDS)


def _shard_files(lake: Path) -> list[Path]:
    stats_root = lsm.stats_dir(lake)
    if not stats_root.exists():
        return []
    return sorted(p for p in stats_root.rglob("*.json") if p.is_file())


def _run_arm(
    lake: Path,
    *,
    viewed: bool,
    appends: int,
    base_rows: int,
) -> tuple[Path, list[float]]:
    """One A/B arm: base write, optional shard seed, then timed appends.

    Returns the partition path + the per-append wall-clock milliseconds. The
    caller controls ON/OFF via the ``BRUTEX_LAKE_STATS_MANIFEST`` env (read
    per call by :func:`lake_stats_manifest.stats_manifest_enabled` — no
    process-cached state to reset).
    """
    writer = ParquetWriter(lake_root=lake)
    base = writer.write(bars=[_bar(_ts(i)) for i in range(base_rows)], **_COORDS)
    if viewed and lsm.stats_manifest_enabled():
        _seed_shard(lake, base)
    timings_ms: list[float] = []
    for i in range(appends):
        bar = _bar(_ts(base_rows + i))
        t0 = time.perf_counter()
        result = writer.write(bars=[bar], **_COORDS)
        timings_ms.append((time.perf_counter() - t0) * 1_000.0)
        assert result.file_rewritten is True  # every append is a real merge rewrite
    return base.path, timings_ms


class TestDeterministicTeeth:
    """Always-run teeth: parity, the anti-storm law, and a genuinely-paying ON arm."""

    def test_rendered_partition_bytes_identical_on_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Golden Rule #7: manifest ON (viewed, hook firing) never touches partition bytes."""
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV, raising=False)
        on_path, _ = _run_arm(tmp_path / "on", viewed=True, appends=5, base_rows=32)
        assert _shard_files(tmp_path / "on"), "the ON arm must have paid the shard rewrites"

        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, lsm.STATS_MANIFEST_OFF)
        off_path, _ = _run_arm(tmp_path / "off", viewed=True, appends=5, base_rows=32)
        assert not _shard_files(tmp_path / "off")

        assert on_path.read_bytes() == off_path.read_bytes()

    def test_unviewed_write_storm_accretes_zero_shards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lazy accretion: a multi-dataset write storm creates NO shard (the prebuild claim)."""
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV, raising=False)
        writer = ParquetWriter(lake_root=tmp_path)
        for interval in ("1minute", "5minute", "15minute", "1hour"):
            coords = dict(_COORDS, interval=interval)
            for i in range(3):
                writer.write(bars=[_bar(_ts(i))], **coords)
        assert _shard_files(tmp_path) == []

    def test_viewed_shard_tracks_every_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ON arm's shard row count moves with every append — the timed cost is real."""
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV, raising=False)
        _run_arm(tmp_path, viewed=True, appends=4, base_rows=8)
        stats = lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS)
        assert stats is not None
        assert stats.row_count == 8 + 4  # base rows + every timed append upserted


class TestWallClockAB:
    """Env-gated wall-clock A/B — the spec §10 bench (b) merge-budget gate."""

    @skip_unless_m4pro
    def test_viewed_write_overhead_within_merge_budget_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Median ON-vs-OFF overhead per append fits under the 12 ms merge-budget floor."""
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, lsm.STATS_MANIFEST_OFF)
        _, off_ms = _run_arm(tmp_path / "off", viewed=True, appends=_TIMED_APPENDS, base_rows=2_000)
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV, raising=False)
        _, on_ms = _run_arm(tmp_path / "on", viewed=True, appends=_TIMED_APPENDS, base_rows=2_000)
        assert _shard_files(tmp_path / "on"), "the ON arm must have paid the shard rewrites"

        overhead_ms = statistics.median(on_ms) - statistics.median(off_ms)
        assert overhead_ms <= _MERGE_BUDGET_FLOOR_MS, (
            f"stats-manifest hook overhead {overhead_ms:.2f} ms/append exceeds the "
            f"{_MERGE_BUDGET_FLOOR_MS} ms merge-budget floor (#946) — adopt the spec §7 "
            "mitigation (upsert after lock release, stat under the lock) or kill-switch"
        )

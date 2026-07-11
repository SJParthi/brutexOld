"""``OptionParquetWriter`` ↔ lake STATS manifest write hooks (DEC-LAKE-STATS-MANIFEST-001).

The options twin of ``test_parquet_writer_stats_manifest.py``: the hook fires
ONLY when bytes are rendered (``_merge_partition``'s ``added > 0`` branch and
the ``heal_partition`` rewrite), stamping ``kind="options"`` rows whose count
is the post-``unique`` DISTINCT ``(side, timestamp)`` height with
``raw_hash=None``; a pure re-pull (``added == 0``), an unchanged heal, and the
kill switch touch NOTHING; an absent shard costs one probe-FIRST existence
check (one key digest + one failed ``os.stat`` — the O(frame) bounds fold and
the ``os.stat(dest)`` never run; review round 2) and is never created by the
writer (lazy accretion); the defensive abstain branches log and skip — never
raise into the pull.

Whitebox per the DEC-P0-017 convention (drives ``_record_option_stats``
directly for the abstain branches), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_stats_manifest as lsm
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
    _record_option_stats,
    _remove_option_stats,
)
from structlog.testing import capture_logs

pytest.importorskip("polars")

_PARTITION_KEY = "NSE_FNO|13|WEEK|0|ATM|CALL"

#: The on-disk series dir ``_safe_segment`` renders for ``_PARTITION_KEY``.
_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"

_SERIES_KEY: dict[str, str] = {"series": _SERIES}

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _option_bar(*, side: str = "CE", minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.57"),
        iv=Decimal("0.1523"),
    )


def _write(lake: Path, bars: list[OptionBar]) -> int:
    return OptionParquetWriter(lake_root=lake).write(
        partition_key=_PARTITION_KEY, coordinate=_COORD, bars=bars
    )


def _dest(lake: Path) -> Path:
    return lake / "options" / _SERIES / "2021-01.parquet"


def _seed_shard(lake: Path, *, rows: int) -> Path:
    """Seed the series' shard from the REAL on-disk partition (the read-side twin)."""
    dest = _dest(lake)
    stat = os.stat(dest)
    row = lsm.StatsFileRow(
        rel=dest.name,
        rows=rows,
        min_ts=datetime(2021, 1, 4, 4, 0, tzinfo=UTC),
        max_ts=datetime(2021, 1, 4, 4, 59, tzinfo=UTC),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        raw_hash=None,
    )
    assert lsm.seed_dataset_stats(lake, kind=lsm.KIND_OPTIONS, key=_SERIES_KEY, files=[row])
    return lsm.stats_shard_path(lake, lsm.KIND_OPTIONS, _SERIES_KEY)


def _shard_row(lake: Path) -> lsm.StatsFileRow:
    stats = lsm.load_shard_stats(lake, kind=lsm.KIND_OPTIONS, key=_SERIES_KEY)
    assert stats is not None
    (row,) = stats.files
    return row


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


class TestHookFires:
    def test_merge_added_upserts_row(self, tmp_path: Path) -> None:
        """An ``added > 0`` merge re-stamps distinct rows / bounds / token."""
        assert _write(tmp_path, [_option_bar(side="CE"), _option_bar(side="PE")]) == 2
        _seed_shard(tmp_path, rows=2)

        assert _write(tmp_path, [_option_bar(side="CE", minute=5)]) == 1
        row = _shard_row(tmp_path)
        assert row.rows == 3  # the post-unique DISTINCT (side, timestamp) count
        assert row.raw_hash is None  # the options writer computes none
        assert row.min_ts == datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        assert row.max_ts == datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
        stat = os.stat(_dest(tmp_path))
        assert (row.mtime_ns, row.size) == (stat.st_mtime_ns, stat.st_size)
        # The FULL anti-illusion validation passes against the live directory.
        valid = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=_SERIES_KEY)
        assert valid is not None
        assert valid.row_count == 3
        assert valid.identity_key == ("side", "timestamp")

    def test_heal_rewrite_restamps(self, tmp_path: Path) -> None:
        """The (side, timestamp) dedup heal re-stamps the post-heal distinct count."""
        import polars as pl

        assert _write(tmp_path, [_option_bar(side="CE"), _option_bar(side="PE")]) == 2
        dest = _dest(tmp_path)
        _seed_shard(tmp_path, rows=2)
        # Poison the stored file with a value-drifted LATER duplicate row.
        df = pl.read_parquet(dest)
        drifted = df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
        pl.concat([df, drifted]).write_parquet(dest)

        heal = OptionParquetWriter(lake_root=tmp_path).heal_partition(
            dest, quarantine_to=tmp_path / "q" / "2021-01.parquet"
        )
        assert heal.changed is True
        row = _shard_row(tmp_path)
        assert row.rows == heal.kept == 2
        stat = os.stat(dest)
        assert (row.mtime_ns, row.size) == (stat.st_mtime_ns, stat.st_size)
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=_SERIES_KEY)

    def test_rust_render_flag_on_token_matches_durable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R11 (DEC-RUST-DATALAKE-IO-ROADMAP-001 PR-2): with the Rust render
        seam ARMED, the recorded stat token still equals the POST-replace
        durable file's ``mtime_ns:size`` — the render → ``os.replace`` →
        ``os.stat`` ordering is unchanged by the seam (the hook fires under
        the SAME held locks, stating the file the routed bytes landed as).
        Crate-free: a faithful fake ``brutex_lake`` (the wheel's own
        defaults render) engages the seam for real."""
        import io
        import sys
        import types

        import polars as pl
        from brutex.data import lake_rust_render as lrr

        def _faithful(ipc_bytes: bytes) -> bytes:
            df = pl.read_ipc(io.BytesIO(ipc_bytes))
            bio = io.BytesIO()
            df.write_parquet(bio)
            return bio.getvalue()

        fake = types.ModuleType("brutex_lake")
        fake.render_parquet_from_ipc = _faithful  # type: ignore[attr-defined]
        fake.render_parquet_defaults_from_ipc = _faithful  # type: ignore[attr-defined]
        fake.pinned_polars_version = lambda: pl.__version__  # type: ignore[attr-defined]
        fake.linked_zstd_version = lambda: "fake"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_lake", fake)
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()
        try:
            assert _write(tmp_path, [_option_bar(side="CE"), _option_bar(side="PE")]) == 2
            _seed_shard(tmp_path, rows=2)
            assert _write(tmp_path, [_option_bar(side="CE", minute=5)]) == 1
            row = _shard_row(tmp_path)
            assert row.rows == 3
            stat = os.stat(_dest(tmp_path))
            assert (row.mtime_ns, row.size) == (stat.st_mtime_ns, stat.st_size)
            assert not lrr.rust_render_disabled()  # the seam genuinely engaged
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=_SERIES_KEY)
        finally:
            lrr.reset_lake_rust_render_for_tests()


class TestHookDoesNotFire:
    def test_pure_repull_added_zero_touches_nothing(self, tmp_path: Path) -> None:
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        shard = _seed_shard(tmp_path, rows=1)
        before = shard.read_bytes()

        assert _write(tmp_path, [_option_bar(side="CE")]) == 0  # pure re-pull
        assert shard.read_bytes() == before  # zero shard touch

    def test_unchanged_heal_touches_nothing(self, tmp_path: Path) -> None:
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        shard = _seed_shard(tmp_path, rows=1)
        before = shard.read_bytes()

        heal = OptionParquetWriter(lake_root=tmp_path).heal_partition(_dest(tmp_path))
        assert heal.changed is False
        assert shard.read_bytes() == before

    def test_kill_switch_off_is_zero_touch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        shard = _seed_shard(tmp_path, rows=1)
        before = shard.read_bytes()
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")

        assert _write(tmp_path, [_option_bar(side="PE", minute=3)]) == 1  # added > 0
        assert shard.read_bytes() == before  # ... but the hook no-opped

    def test_lazy_accretion_absent_shard_single_probe_no_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probes: list[Path] = []
        real_probe = lsm._shard_exists

        def _counting(path: Path) -> bool:
            probes.append(path)
            return real_probe(path)

        monkeypatch.setattr(lsm, "_shard_exists", _counting)
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1

        assert len(probes) == 1
        assert probes[0] == lsm.stats_shard_path(tmp_path, lsm.KIND_OPTIONS, _SERIES_KEY)
        assert not probes[0].exists()  # never created by the writer
        assert not lsm.stats_dir(tmp_path).exists()


class TestBestEffortAbstains:
    def test_kill_switch_direct_call_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars as pl

        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        with capture_logs() as logs:
            _record_option_stats(
                tmp_path, dest=_dest(tmp_path), frame=pl.DataFrame({"timestamp": []})
            )
        assert logs == []  # a pure no-op — not even a warning

    def test_underivable_bounds_are_logged_and_skipped(self, tmp_path: Path) -> None:
        """An empty/null timestamp column (defensive) warns — never raises.

        The shard must EXIST to reach the bounds fold (the hook probes shard
        existence FIRST — review round 2), so the fixture seeds it.
        """
        import polars as pl

        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        _seed_shard(tmp_path, rows=1)
        frame = pl.DataFrame(
            {"timestamp": []},
            schema={"timestamp": pl.Datetime(time_unit="us", time_zone="UTC")},
        )
        with capture_logs() as logs:
            _record_option_stats(tmp_path, dest=_dest(tmp_path), frame=frame)
        assert len(_events(logs, "lake.stats_manifest.bounds_underivable")) == 1

    def test_stat_failure_is_logged_and_skipped(self, tmp_path: Path) -> None:
        """A vanished dest at the hook's ``os.stat`` warns — never raises.

        The GHOST dest sits under the SAME (shard-seeded) series, so the
        probe-first hook flows through to its own ``os.stat(dest)``.
        """
        import polars as pl

        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        _seed_shard(tmp_path, rows=1)
        ghost = tmp_path / "options" / _SERIES / "2021-02.parquet"
        frame = pl.DataFrame(
            {"timestamp": [datetime(2021, 1, 4, 4, 0, tzinfo=UTC)]},
            schema={"timestamp": pl.Datetime(time_unit="us", time_zone="UTC")},
        )
        with capture_logs() as logs:
            _record_option_stats(tmp_path, dest=ghost, frame=frame)
        assert len(_events(logs, "lake.stats_manifest.stat_failed")) == 1

    def test_absent_shard_probe_first_touches_neither_frame_nor_dest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe-FIRST lazy accretion (review round 2, fails-before): with NO
        shard the hook returns after ONE shard-path probe — the O(frame)
        bounds fold never runs (no defensive warning even on a null-bounds
        frame) and ``dest`` is never statted."""
        import polars as pl

        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        statted: list[str] = []
        real = os.stat

        def _rec(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            statted.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _rec)
        # A frame whose bounds fold WOULD warn — proving the fold never ran.
        frame = pl.DataFrame(
            {"timestamp": []},
            schema={"timestamp": pl.Datetime(time_unit="us", time_zone="UTC")},
        )
        with capture_logs() as logs:
            _record_option_stats(tmp_path, dest=_dest(tmp_path), frame=frame)
        assert logs == []  # no bounds_underivable — the fold never ran
        assert str(_dest(tmp_path)) not in statted  # dest never statted
        shard = str(lsm.stats_shard_path(tmp_path, lsm.KIND_OPTIONS, _SERIES_KEY))
        assert statted.count(shard) == 1  # exactly the one probe


class TestWhollyRemovedRemoveHook:
    """The ``kept == 0`` quarantine-MOVE drops the unlinked file's shard row.

    The options twin of ``ParquetWriter._remove_stats_hook`` (merge-time
    composition of DEC-LAKE-STATS-MANIFEST-001 with
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001's wholly-removed branch, which
    post-dates the manifest PR): without the hook the shard would fail the
    read-side file-set gate on every consult until a reseed — safe (fallback,
    never a wrong answer) but defeating the manifest for the series.
    """

    def test_wholly_removed_heal_drops_shard_row(self, tmp_path: Path) -> None:
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        dest = _dest(tmp_path)
        shard = _seed_shard(tmp_path, rows=1)
        heal = OptionParquetWriter(lake_root=tmp_path).heal_partition(
            dest,
            remove_keys={("CE", datetime(2021, 1, 4, 4, 0, tzinfo=UTC))},
            quarantine_to=tmp_path / "q" / "2021-01.parquet",
        )
        assert heal.changed is True
        assert heal.kept == 0
        assert not dest.exists()  # quarantine-MOVED away
        # The LAST row's removal unlinks the whole shard (an empty dataset
        # has no shard) — the read side never sees a stale file-set.
        assert not shard.exists()

    def test_kill_switch_off_remove_is_pure_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _write(tmp_path, [_option_bar(side="CE")]) == 1
        shard = _seed_shard(tmp_path, rows=1)
        before = shard.read_bytes()
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        with capture_logs() as logs:
            _remove_option_stats(tmp_path, dest=_dest(tmp_path))
        assert logs == []  # a pure no-op — not even a probe warning
        assert shard.read_bytes() == before

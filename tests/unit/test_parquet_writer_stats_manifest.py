"""``ParquetWriter`` ↔ lake STATS manifest write hooks (DEC-LAKE-STATS-MANIFEST-001).

Stage-2 writer-hook pins over the bars tree: the hook fires on every path that
RENDERS partition bytes (fresh write · merge rewrite · the ``bars_added == 0``
latest-wins restate · heal rewrite) and NEVER on a byte-untouched path (merge
no-op · keep-first conflict-only merge · heal probe-failure abstain ·
kill-switch off). Keying is the NEW explicit ``WriteResult.file_rewritten``
flag — bytes rendered, never ``bars_added``. Every hook is best-effort (a
shard failure logs + unlinks + returns; the write NEVER raises), lazily
accreting (an ABSENT shard costs exactly ONE probe-FIRST existence check —
one key digest + one failed ``os.stat``, before the hook even stats *dest* —
and is never created by the writer; review round 2), and captures its stat
token INSIDE the still-held partition flock.

Whitebox per the DEC-P0-017 convention (drives ``_record_stats_hook`` /
``_remove_stats_hook`` / ``_stats_key_for_dest`` directly for the abstain
branches), so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import fcntl
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_stats_manifest as lsm
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.partition_lock import partition_lock_path
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

#: The one bars dataset every test writes into (canonical engine coords).
_COORDS: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None
    )


def _ts(minute: int, *, month: int = 1, day: int = 4) -> datetime:
    return datetime(2021, month, day, 4, minute, tzinfo=UTC)


def _seed_shard_from_result(lake: Path, result: WriteResult) -> Path:
    """Seed the dataset's shard from one real ``WriteResult`` (the read-side twin)."""
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


def _shard_rows(lake: Path) -> dict[str, lsm.StatsFileRow]:
    stats = lsm.load_shard_stats(lake, kind=lsm.KIND_BARS, key=_COORDS)
    assert stats is not None
    return {row.rel: row for row in stats.files}


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


def _poison_duplicate(dest: Path, rows: Sequence[tuple[datetime, float]]) -> None:
    """Raw-pyarrow plant of VERBATIM rows (duplicates included) — the #1067 way."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


class TestFileRewrittenFlag:
    """The ``WriteResult.file_rewritten`` truth table (bytes RENDERED, never bars_added)."""

    def test_truth_table(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        fresh = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        assert fresh.file_rewritten is True  # fresh write renders bytes

        added = writer.write(bars=[_bar(_ts(1))], **_COORDS)
        assert added.file_rewritten is True  # merge that added a bar
        assert added.bars_added == 1

        noop = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        assert noop.file_rewritten is False  # byte-identical no-op
        assert noop.bars_added == 0

        conflict = writer.write(bars=[_bar(_ts(0), close="999.00")], **_COORDS)
        assert conflict.file_rewritten is False  # keep-first: stored bytes kept
        assert conflict.bars_added == 0
        assert len(conflict.conflicts) == 1

        restate = writer.write(
            bars=[_bar(_ts(0), close="999.00")], merge_mode="latest-wins", **_COORDS
        )
        assert restate.file_rewritten is True  # bytes restated with bars_added == 0
        assert restate.bars_added == 0


class TestHookFires:
    def test_fresh_write_upserts_row(self, tmp_path: Path) -> None:
        """A fresh partition write into a SEEDED dataset gains its shard row."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0)), _bar(_ts(1))], **_COORDS)
        _seed_shard_from_result(tmp_path, jan)

        feb = writer.write(bars=[_bar(_ts(0, month=2))], **_COORDS)
        assert feb.file_rewritten is True
        rows = _shard_rows(tmp_path)
        assert set(rows) == {"2021/01.parquet", "2021/02.parquet"}
        stamped = rows["2021/02.parquet"]
        stat = os.stat(feb.path)
        assert stamped.rows == feb.row_count == 1
        assert stamped.min_ts == feb.first_ts
        assert stamped.max_ts == feb.last_ts
        assert stamped.mtime_ns == stat.st_mtime_ns
        assert stamped.size == stat.st_size
        assert stamped.raw_hash == feb.raw_hash
        # The FULL anti-illusion validation passes — file set, tokens, months.
        valid = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS)
        assert valid is not None
        assert valid.row_count == 3

    def test_merge_rewrite_updates_row(self, tmp_path: Path) -> None:
        """A merge that adds bars re-stamps rows / bounds / raw_hash / token."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        _seed_shard_from_result(tmp_path, jan)
        before = _shard_rows(tmp_path)["2021/01.parquet"]

        merged = writer.write(bars=[_bar(_ts(5))], **_COORDS)
        assert merged.file_rewritten is True
        after = _shard_rows(tmp_path)["2021/01.parquet"]
        assert after.rows == merged.row_count == 2
        assert after.raw_hash == merged.raw_hash != before.raw_hash
        assert after.max_ts == _ts(5)
        stat = os.stat(merged.path)
        assert (after.mtime_ns, after.size) == (stat.st_mtime_ns, stat.st_size)
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS) is not None

    def test_latest_wins_restate_with_zero_added_is_recorded(self, tmp_path: Path) -> None:
        """The Greeks-enrich-shaped restate (bars_added == 0, bytes changed) re-stamps."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        _seed_shard_from_result(tmp_path, jan)
        before = _shard_rows(tmp_path)["2021/01.parquet"]

        restate = writer.write(
            bars=[_bar(_ts(0), close="123.45")], merge_mode="latest-wins", **_COORDS
        )
        assert restate.bars_added == 0
        assert restate.file_rewritten is True
        after = _shard_rows(tmp_path)["2021/01.parquet"]
        assert after.raw_hash == restate.raw_hash != before.raw_hash
        assert after.rows == 1
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS) is not None

    def test_stat_token_is_captured_under_the_partition_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook runs INSIDE the held partition flock and stamps the durable stat.

        A separate fd (≈ another process) probes the partition lock DURING the
        hook call and must find it HELD — so no concurrent ``os.replace`` can
        slip between the data rename and the token stat.
        """
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        _seed_shard_from_result(tmp_path, jan)

        seen: dict[str, object] = {}
        real_record = lsm.record_partition_stats

        def _spy(lake_root: Path, **kwargs: object) -> bool:
            dest = kwargs["dest"]
            assert isinstance(dest, Path)
            lock_path = partition_lock_path(tmp_path, dest)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                seen["flock_held"] = True
            else:  # pragma: no cover — only on a lock-scope regression
                fcntl.flock(fd, fcntl.LOCK_UN)
                seen["flock_held"] = False
            finally:
                os.close(fd)
            seen["stat"] = kwargs["stat"]
            return real_record(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.parquet_writer.record_partition_stats", _spy)
        result = writer.write(bars=[_bar(_ts(9))], **_COORDS)

        assert seen["flock_held"] is True
        stat = seen["stat"]
        assert isinstance(stat, os.stat_result)
        final = os.stat(result.path)
        assert (stat.st_mtime_ns, stat.st_size) == (final.st_mtime_ns, final.st_size)

    def test_heal_rewrite_restamps_with_rotated_hash(self, tmp_path: Path) -> None:
        """The heal rewrite upserts post-heal rows + the ROTATED raw_hash."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0)), _bar(_ts(1))], **_COORDS)
        _seed_shard_from_result(tmp_path, jan)
        # Poison the stored file with a physical duplicate (predates the
        # write-side uniqueness invariant) — the shard token is now stale.
        _poison_duplicate(jan.path, [(_ts(0), 100.0), (_ts(0), 999.0), (_ts(1), 100.0)])

        heal = writer.heal_partition(jan.path, quarantine_to=tmp_path / "q" / "01.parquet")
        assert heal.changed is True
        assert heal.write is not None
        assert heal.write.file_rewritten is True
        row = _shard_rows(tmp_path)["2021/01.parquet"]
        assert row.rows == heal.kept == 2
        assert row.raw_hash == heal.raw_hash_after != heal.raw_hash_before
        stat = os.stat(jan.path)
        assert (row.mtime_ns, row.size) == (stat.st_mtime_ns, stat.st_size)
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS) is not None

    def test_heal_quarantine_move_removes_row(self, tmp_path: Path) -> None:
        """A quarantine-MOVE (every row removed) deletes the file's shard row."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        feb = writer.write(bars=[_bar(_ts(0, month=2))], **_COORDS)
        stat_jan = os.stat(jan.path)
        stat_feb = os.stat(feb.path)
        rows = [
            lsm.StatsFileRow(
                rel="2021/01.parquet",
                rows=1,
                min_ts=jan.first_ts,
                max_ts=jan.last_ts,
                mtime_ns=stat_jan.st_mtime_ns,
                size=stat_jan.st_size,
                raw_hash=jan.raw_hash,
            ),
            lsm.StatsFileRow(
                rel="2021/02.parquet",
                rows=1,
                min_ts=feb.first_ts,
                max_ts=feb.last_ts,
                mtime_ns=stat_feb.st_mtime_ns,
                size=stat_feb.st_size,
                raw_hash=feb.raw_hash,
            ),
        ]
        assert lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS, files=rows)

        heal = writer.heal_partition(
            feb.path,
            remove_timestamps={_ts(0, month=2)},
            quarantine_to=tmp_path / "q" / "02.parquet",
        )
        assert heal.changed is True
        assert heal.raw_hash_after is None  # quarantine-MOVED away
        assert set(_shard_rows(tmp_path)) == {"2021/01.parquet"}
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=_COORDS) is not None

    def test_heal_quarantine_move_of_last_row_unlinks_shard(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)

        heal = writer.heal_partition(
            jan.path, remove_timestamps={_ts(0)}, quarantine_to=tmp_path / "q" / "01.parquet"
        )
        assert heal.changed is True
        assert not shard.exists()  # an empty dataset has NO shard


class TestHookDoesNotFire:
    def test_merge_noop_touches_nothing(self, tmp_path: Path) -> None:
        """The byte-identical re-pull leaves the shard bytes UNTOUCHED."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)
        before = shard.read_bytes()

        noop = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        assert noop.file_rewritten is False
        assert shard.read_bytes() == before

    def test_kill_switch_off_is_zero_touch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BRUTEX_LAKE_STATS_MANIFEST=off`` ⇒ no shard mutation on ANY rewrite."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)
        before = shard.read_bytes()
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")

        merged = writer.write(bars=[_bar(_ts(7))], **_COORDS)
        assert merged.file_rewritten is True  # the flag is honest either way
        assert shard.read_bytes() == before  # ... but the hook no-opped

        heal = writer.heal_partition(
            jan.path, remove_timestamps={_ts(0), _ts(7)}, quarantine_to=tmp_path / "q" / "01.pq"
        )
        assert heal.changed is True
        assert shard.read_bytes() == before  # the remove hook no-opped too

    def test_lazy_accretion_absent_shard_single_probe_no_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No shard ⇒ ONE existence probe per write and the writer creates nothing."""
        probes: list[Path] = []
        real_probe = lsm._shard_exists

        def _counting(path: Path) -> bool:
            probes.append(path)
            return real_probe(path)

        monkeypatch.setattr(lsm, "_shard_exists", _counting)
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(bars=[_bar(_ts(0))], **_COORDS)

        assert len(probes) == 1  # exactly one os.stat-shaped probe
        assert probes[0] == lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, _COORDS)
        assert not probes[0].exists()  # never created by the writer
        assert not lsm.stats_dir(tmp_path).exists()  # no stats tree either

    def test_absent_shard_probe_first_never_stats_dest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe-FIRST lazy accretion (review round 2, fails-before): with NO
        shard the hook's cost is the one shard-path probe — ``dest`` itself is
        never statted (the pre-fix hook statted dest before probing)."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        statted: list[str] = []
        real = os.stat

        def _rec(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            statted.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _rec)
        writer._record_stats_hook(jan.path, jan)
        assert str(jan.path) not in statted  # dest never statted
        shard = str(lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, _COORDS))
        assert statted.count(shard) == 1  # exactly the one probe

    def test_heal_probe_failure_abstains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #1067 abstain (failed footer probe) touches neither file nor shard."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)
        before = shard.read_bytes()
        monkeypatch.setattr(ParquetWriter, "_stored_physical_rows", staticmethod(lambda d, b: None))

        heal = writer.heal_partition(jan.path, quarantine_to=tmp_path / "q" / "01.pq")
        assert heal.changed is False
        assert shard.read_bytes() == before


class TestBestEffortNeverRaises:
    def test_shard_lock_timeout_never_raises_into_the_pull(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged shard flock costs the shard (unlinked), never the write."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "0.05")

        lock_path = partition_lock_path(tmp_path, shard)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with capture_logs() as logs:
                result = writer.write(bars=[_bar(_ts(3))], **_COORDS)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert result.file_rewritten is True  # the DATA write succeeded
        assert result.bars_added == 1
        assert len(_events(logs, "lake.stats_manifest.record_failed")) == 1
        assert not shard.exists()  # best-effort law: unlink, reader falls back

    def test_stat_failure_on_dest_is_logged_and_skipped(self, tmp_path: Path) -> None:
        """A vanished dest at the hook's own ``os.stat`` warns — never raises."""
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        shard = _seed_shard_from_result(tmp_path, jan)
        before = shard.read_bytes()
        ghost = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2021" / "03.parquet"
        with capture_logs() as logs:
            writer._record_stats_hook(ghost, jan)
        assert len(_events(logs, "lake.stats_manifest.stat_failed")) == 1
        assert shard.read_bytes() == before

    def test_record_hook_abstains_on_underivable_key(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        jan = writer.write(bars=[_bar(_ts(0))], **_COORDS)
        with capture_logs() as logs:
            writer._record_stats_hook(tmp_path / "elsewhere" / "01.parquet", jan)
        assert len(_events(logs, "lake.stats_manifest.key_underivable")) == 1

    def test_remove_hook_abstains_on_underivable_key(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            writer._remove_stats_hook(tmp_path / "elsewhere" / "01.parquet")
        assert len(_events(logs, "lake.stats_manifest.key_underivable")) == 1

    def test_remove_hook_kill_switch_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            writer._remove_stats_hook(tmp_path / "elsewhere" / "01.parquet")
        assert logs == []  # not even the abstain warning — a pure no-op


class TestStatsKeyForDest:
    """The ONE verbatim path→key derivation both hook sites share."""

    def test_canonical_path_yields_verbatim_key(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        dest = tmp_path / "bars" / "NSE" / "CASH" / "NSE-M&M" / "1minute" / "2021" / "01.parquet"
        assert writer._stats_key_for_dest(dest) == {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-M&M",  # a #911-drift dir stays its own dataset
            "interval": "1minute",
        }

    def test_outside_bars_tree_is_none(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        assert writer._stats_key_for_dest(Path("/absolutely/elsewhere/01.parquet")) is None
        assert writer._stats_key_for_dest(tmp_path / "options" / "S" / "2021-01.parquet") is None

    def test_wrong_depth_is_none(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        assert writer._stats_key_for_dest(tmp_path / "bars" / "NSE" / "01.parquet") is None

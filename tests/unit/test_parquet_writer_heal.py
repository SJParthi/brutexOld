"""The public partition-heal surface of :class:`ParquetWriter` (DEC-LAKE-HEAL-001).

PR #1067 (DEC-LAKE-WRITE-UNIQUE-001) heals a poisoned partition ONLY when a
write touches it — a SETTLED month nobody re-pulls stayed poisoned forever.
:meth:`ParquetWriter.heal_partition` is the new PUBLIC entry the lake healer
drives: the SAME mode-independent keep-FIRST collapse through the SAME
in-module privates (locks in ``write()``'s order, the overlay-preserving
decode, the P9 tripwire, the deterministic render), plus the row-REMOVING
``remove_timestamps`` hook the foreign-row reassignment uses, plus the
MANDATORY quarantine-copy of the original bytes before any rewrite.
:meth:`ParquetWriter.read_partition_bars` is the healer's consistent-read twin.

This suite pins: keep-first winner values, byte-identical no-op idempotence,
probe-failure never-guess, quarantine-copy restorability, row-removal-only
(kept values untouched), the emptied-file quarantine-MOVE, refusal without a
quarantine destination, ENOSPC → graceful ``LakeWriteHalt`` with the original
untouched, the cross-process flock held DURING the heal, Greeks-overlay
round-trip, the P9 call ordering, and the raw-hash rotation feeding
``make_catalog_entry``. New file (the #1067 pin suite
``test_parquet_writer_dedup.py`` is NOT modified).

Whitebox: monkeypatches private helpers (the DEC-P0-017 convention), so the
file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data.catalog import Catalog
from brutex.data.errors import LakeWriteHalt, WriterError
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import ParquetWriter, PartitionHealResult
from brutex.data.partition import build_partition_path
from brutex.data.partition_lock import partition_lock_path
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")


_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _ts(minute: int) -> datetime:
    """A UTC 1-minute instant inside the June-2024 partition."""
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _bar(minute: int, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    """A flat bar at :func:`_ts`\\ (*minute*) whose OHLC all equal *close*."""
    c = Decimal(close)
    return CandleBar(
        timestamp=_ts(minute),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _dest(tmp_path: Path) -> Path:
    return build_partition_path(
        lake_root=tmp_path,
        exchange=_COORDS["exchange"],
        segment=_COORDS["segment"],
        symbol=_COORDS["symbol"],
        interval=_COORDS["interval"],
        year=2024,
        month=6,
    )


def _quarantine(tmp_path: Path, dest: Path) -> Path:
    """The quarantine destination preserving *dest*'s lake-relative path."""
    return tmp_path / ".quarantine" / "20240701T000000Z" / dest.relative_to(tmp_path)


def _poison_partition(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Write a RAW pyarrow partition (the operator's poisoned lake).

    Duplicate timestamps are written VERBATIM — the fixed writer refuses
    them, so poison must be planted with raw pyarrow (the #1067 convention).
    """
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


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


def _stored_frame(dest: Path) -> Any:
    import polars as pl

    return pl.read_parquet(dest).sort("timestamp")


def _flock_held_by_other_fd(lock_path: Path) -> bool:
    """True iff the flock at *lock_path* is held by another open file description."""
    fd = os.open(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# ── the keep-first dedup heal ──────────────────────────────────────────────


class TestHealPartitionDedup:
    def test_heal_partition_collapses_dups_keep_first(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: no public heal entry exists — a settled
        month's duplicates persist unless a write happens to touch it."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        # First physical ts0 row carries 100.0 — the keep-FIRST winner to prove.
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        with capture_logs() as logs:
            result = writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))
        assert result.changed is True
        assert result.kept == 2
        assert result.dropped_duplicates == 1
        assert result.dropped_removed == 0
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1)]
        assert stored["close"].to_list() == [100.0, 101.0]  # FIRST physical row wins
        healed = _events(logs, "lake.partition.dedup_healed")
        assert len(healed) == 1
        assert healed[0]["log_level"] == "warning"
        assert healed[0]["kept"] == 2  # the same pinned fields as the merge self-heal
        assert healed[0]["dropped"] == 1
        assert healed[0]["partition"] == str(dest)

    def test_heal_partition_noop_is_byte_identical(self, tmp_path: Path) -> None:
        """A clean file is left byte-untouched — the idempotence oracle (Rule #7)."""
        writer = ParquetWriter(lake_root=tmp_path)
        write = writer.write(bars=[_bar(0), _bar(1)], **_COORDS)
        before_bytes = write.path.read_bytes()
        quarantine_to = _quarantine(tmp_path, write.path)
        result = writer.heal_partition(write.path, quarantine_to=quarantine_to)
        assert result.changed is False
        assert result.kept == 2
        assert result.dropped_duplicates == 0
        assert result.dropped_removed == 0
        assert result.write is None
        assert write.path.read_bytes() == before_bytes
        assert result.raw_hash_before == result.raw_hash_after == write.raw_hash
        assert not quarantine_to.exists()  # a no-op heal makes NO quarantine copy

    def test_heal_partition_probe_failure_never_guesses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #1067 pin: a failed footer probe SKIPS the heal — the poison
        persists (logged upstream) rather than being guessed at."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 100.0, 10)])
        poisoned_bytes = dest.read_bytes()
        monkeypatch.setattr(writer, "_stored_physical_rows", lambda _dest, _existing: None)
        result = writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))
        assert result.changed is False
        assert result.write is None
        assert result.raw_hash_after == result.raw_hash_before
        assert dest.read_bytes() == poisoned_bytes  # untouched — never mis-healed

    def test_heal_quarantine_copy_before_rewrite_is_restorable(self, tmp_path: Path) -> None:
        """The quarantine copy is byte-equal to the ORIGINAL poisoned bytes —
        restore (copy back) reproduces the pre-heal file exactly."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        original_bytes = dest.read_bytes()
        quarantine_to = _quarantine(tmp_path, dest)
        result = writer.heal_partition(dest, quarantine_to=quarantine_to)
        assert result.changed is True
        assert quarantine_to.read_bytes() == original_bytes
        assert result.raw_hash_before == hashlib.sha256(original_bytes).hexdigest()
        # Restore = a plain copy back; the file is byte-identical to pre-heal.
        dest.write_bytes(quarantine_to.read_bytes())
        assert dest.read_bytes() == original_bytes


# ── row removal (the reassignment hook) ────────────────────────────────────


class TestHealPartitionRemoveTimestamps:
    def test_heal_remove_timestamps_drops_rows_only(self, tmp_path: Path) -> None:
        """Row-REMOVING law: kept rows keep their exact stored values; an
        absent remove timestamp is ignored."""
        writer = ParquetWriter(lake_root=tmp_path)
        write = writer.write(
            bars=[_bar(0, close="100.25"), _bar(1, close="101.50"), _bar(2, close="102.75")],
            **_COORDS,
        )
        with capture_logs() as logs:
            result = writer.heal_partition(
                write.path,
                remove_timestamps=frozenset({_ts(1), _ts(59)}),  # ts59 is absent — ignored
                quarantine_to=_quarantine(tmp_path, write.path),
            )
        assert result.changed is True
        assert result.dropped_removed == 1
        assert result.dropped_duplicates == 0
        assert result.kept == 2
        stored = _stored_frame(write.path)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(2)]
        assert stored["close"].to_list() == [100.25, 102.75]  # values untouched
        # A pure removal (no stored dups) never claims a dedup heal.
        assert _events(logs, "lake.partition.dedup_healed") == []

    def test_heal_emptied_file_quarantine_moved(self, tmp_path: Path) -> None:
        """Every row removed (a wholly-foreign file) → the file is quarantine-
        MOVED: dest gone, the ``<yyyy>/`` dir pruned, ``raw_hash_after=None``."""
        writer = ParquetWriter(lake_root=tmp_path)
        write = writer.write(bars=[_bar(0), _bar(1)], **_COORDS)
        original_bytes = write.path.read_bytes()
        quarantine_to = _quarantine(tmp_path, write.path)
        result = writer.heal_partition(
            write.path,
            remove_timestamps=frozenset({_ts(0), _ts(1)}),
            quarantine_to=quarantine_to,
        )
        assert result.changed is True
        assert result.kept == 0
        assert result.dropped_removed == 2
        assert result.raw_hash_after is None
        assert result.write is None
        assert not write.path.exists()
        assert not write.path.parent.exists()  # the now-empty <yyyy>/ dir pruned
        assert quarantine_to.read_bytes() == original_bytes

    def test_heal_missing_quarantine_dest_refused(self, tmp_path: Path) -> None:
        """A changing heal with no quarantine destination is REFUSED — never
        rewrite without the safety copy — and the file is untouched."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        poisoned_bytes = dest.read_bytes()
        with pytest.raises(WriterError, match="quarantine destination"):
            writer.heal_partition(dest)
        assert dest.read_bytes() == poisoned_bytes


# ── failure envelopes ──────────────────────────────────────────────────────


class TestHealPartitionFailureEnvelope:
    def test_heal_disk_full_graceful_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENOSPC at the quarantine copy → ``LakeWriteHalt("disk_full")`` with
        the ORIGINAL partition byte-untouched (graceful pull-wide stop)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        poisoned_bytes = dest.read_bytes()

        def _mkstemp_enospc(*_args: object, **_kwargs: object) -> tuple[int, str]:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.tempfile.mkstemp", _mkstemp_enospc)
        with pytest.raises(LakeWriteHalt) as excinfo:
            writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))
        assert excinfo.value.reason == "disk_full"
        assert dest.read_bytes() == poisoned_bytes

    def test_heal_vanished_file_raises_quarantinable_writer_error(self, tmp_path: Path) -> None:
        """A vanished partition raises the quarantinable WriterError the read
        privates already map (the healer counts it, batch continues)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(WriterError, match="unreadable existing partition"):
            writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))

    def test_heal_holds_partition_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross-process advisory flock is HELD while the heal body runs —
        a second file description's ``LOCK_EX|LOCK_NB`` raises ``BlockingIOError``
        (the test_partition_lock second-fd technique)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        lock_path = partition_lock_path(tmp_path, dest)
        real_probe = ParquetWriter._stored_physical_rows
        held_during_heal: list[bool] = []

        def _probing_rows(target: Path, existing: bytes) -> int | None:
            held_during_heal.append(_flock_held_by_other_fd(lock_path))
            return real_probe(target, existing)

        monkeypatch.setattr(writer, "_stored_physical_rows", _probing_rows)
        result = writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))
        assert result.changed is True
        assert held_during_heal == [True]
        assert _flock_held_by_other_fd(lock_path) is False  # released after

    def test_heal_p9_tripwire_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The P9 assert runs BETWEEN the quarantine copy and the render — a
        firing tripwire aborts the rewrite with dest byte-untouched (the
        quarantine copy already exists, which is safe: originals only)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11)])
        poisoned_bytes = dest.read_bytes()

        def _tripwire(target: Path, _bars: list[CandleBar]) -> None:
            raise WriterError(f"write invariant violated for partition {target}")

        monkeypatch.setattr(writer, "_assert_strictly_increasing", _tripwire)
        quarantine_to = _quarantine(tmp_path, dest)
        with pytest.raises(WriterError, match="write invariant violated"):
            writer.heal_partition(dest, quarantine_to=quarantine_to)
        assert dest.read_bytes() == poisoned_bytes  # refused to render
        assert quarantine_to.read_bytes() == poisoned_bytes  # copy-first ordering


# ── Greeks overlay round-trip ──────────────────────────────────────────────


class TestHealPreservesGreeksOverlay:
    def test_heal_preserves_greeks_overlay(self, tmp_path: Path) -> None:
        """A 17-column Greeks-enriched partition heals with the kept rows'
        overlay INTACT (the overlay-preserving decode + render round-trip)."""
        writer = ParquetWriter(lake_root=tmp_path)
        greek_bar = FnoGreeksBar(
            timestamp=_ts(0),
            open=Decimal("100.00"),
            high=Decimal("100.00"),
            low=Decimal("100.00"),
            close=Decimal("100.00"),
            volume=10,
            open_interest=500,
            iv=0.18,
            delta=0.52,
            gamma=0.002,
            theta=-4.1,
            vega=9.5,
            rho=1.2,
            spot_at_bar=Decimal("23500.50"),
            t_years_used=0.05,
            rate_used=0.07,
            greeks_provenance_id=7,
        )
        plain_bar = _bar(1, close="101.00")
        write = writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-24000-CE",
            interval="1minute",
            bars=[greek_bar, plain_bar],
        )
        result = writer.heal_partition(
            write.path,
            remove_timestamps=frozenset({_ts(1)}),
            quarantine_to=_quarantine(tmp_path, write.path),
        )
        assert result.changed is True
        survivors = writer.read_partition_bars(write.path)
        assert set(survivors) == {_ts(0)}
        kept = survivors[_ts(0)]
        assert isinstance(kept, FnoGreeksBar)
        assert kept.iv == 0.18
        assert kept.delta == 0.52
        assert kept.spot_at_bar == Decimal("23500.50")
        assert kept.greeks_provenance_id == 7


# ── read_partition_bars ────────────────────────────────────────────────────


class TestReadPartitionBars:
    def test_read_partition_bars_collapses_and_locks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consistent read collapses stored dups keep-FIRST (logged) and
        holds the SAME cross-process flock the writers take."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        lock_path = partition_lock_path(tmp_path, dest)
        real_decode = ParquetWriter._read_existing_bars
        held_during_read: list[bool] = []

        def _probing_decode(target: Path, existing: bytes) -> dict[datetime, CandleBar]:
            held_during_read.append(_flock_held_by_other_fd(lock_path))
            return real_decode(target, existing)

        monkeypatch.setattr(writer, "_read_existing_bars", _probing_decode)
        with capture_logs() as logs:
            bars = writer.read_partition_bars(dest)
        assert held_during_read == [True]
        assert _flock_held_by_other_fd(lock_path) is False
        assert sorted(bars) == [_ts(0), _ts(1)]
        assert bars[_ts(0)].close == Decimal("100.00")  # first physical row wins
        collapsed = _events(logs, "lake.partition.duplicate_collapsed")
        assert len(collapsed) == 1
        assert collapsed[0]["physical_rows"] == 3
        assert collapsed[0]["unique_timestamps"] == 2


# ── raw-hash rotation + catalog entry ──────────────────────────────────────


class TestHealRawHashRotation:
    def test_heal_raw_hash_rotates_and_catalog_entry_valid(self, tmp_path: Path) -> None:
        """The healed bytes' sha256 ROTATES onto ``result.write.raw_hash`` and
        ``make_catalog_entry(result=heal.write)`` upserts cleanly — a healed
        partition can never masquerade as untouched (Golden Rule #4)."""
        pytest.importorskip("duckdb")
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        result = writer.heal_partition(dest, quarantine_to=_quarantine(tmp_path, dest))
        assert result.changed is True
        assert result.write is not None
        assert result.raw_hash_after == result.write.raw_hash
        assert result.raw_hash_after != result.raw_hash_before
        assert result.write.raw_hash == hashlib.sha256(dest.read_bytes()).hexdigest()
        assert result.write.bars_added == 0  # a heal never adds bars
        entry = writer.make_catalog_entry(result=result.write, **_COORDS)
        assert entry.year == 2024
        assert entry.month == 6
        assert entry.row_count == 2
        with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
            catalog.upsert(entry)
            stored = catalog.get(year=2024, month=6, **_COORDS)
        assert stored is not None
        assert stored.raw_hash == result.write.raw_hash

    def test_heal_result_type_shape(self, tmp_path: Path) -> None:
        """The result is the frozen value type the healer's report reads."""
        writer = ParquetWriter(lake_root=tmp_path)
        write = writer.write(bars=[_bar(0)], **_COORDS)
        result = writer.heal_partition(write.path, quarantine_to=None)
        assert isinstance(result, PartitionHealResult)
        with pytest.raises(AttributeError):
            result.changed = True  # type: ignore[misc] — frozen dataclass pin

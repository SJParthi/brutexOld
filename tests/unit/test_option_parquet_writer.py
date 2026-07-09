"""Tests for `brutex.data.option_parquet_writer` — the expired-options table.

100% line+branch via a REAL temp lake (polars round-trip, no mocks): write +
read back, the 2dp money rounding (with full-precision iv), idempotent
re-pull (0 new), partial merge, month-split, empty input, the path-safety
guards, and the corrupt-existing-partition quarantine + bounded lock registry.

Whitebox: drives the private ``_PARTITION_LOCKS`` registry, so per the
DEC-P0-017 convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
    OptionWriterError,
)
from brutex.data.partition_lock import partition_lock_path


def _partition_lock_is_held(lock_path: Path) -> bool:
    """True iff a SEPARATE fd cannot take the partition's exclusive lock (it is held).

    A second ``open`` is a distinct open file description, so ``flock`` treats it
    like another PROCESS — the cross-process contention the lake lock must win
    (DEC-LAKE-PARTITION-FLOCK-001).
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _bar(
    *, side: str = "CE", minute: int = 0, month: int = 1, strike: str | None = "17500.005"
) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, month, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.005"),  # 3dp → must round to 100.01
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.004"),  # → 105.00
        volume=250,
        open_interest=1200,
        side=side,
        strike=None if strike is None else Decimal(strike),
        spot=Decimal("17480.567"),  # → 17480.57
        iv=Decimal("0.1523"),  # NOT money → keeps 4dp
    )


def _writer(tmp_path: Path) -> OptionParquetWriter:
    return OptionParquetWriter(lake_root=tmp_path)


def _read(tmp_path: Path, segment: str = "NSE_FNO__13__WEEK__0__ATM__CALL") -> pl.DataFrame:
    (f,) = (tmp_path / "options" / segment).glob("*.parquet")
    return pl.read_parquet(f)


class TestWrite:
    def test_write_rounds_money_keeps_iv_precision(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        n = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        assert n == 1
        df = _read(tmp_path)
        assert df["open"][0] == 100.01  # 100.005 → 100.01 (half-up, 2dp)
        assert df["close"][0] == 105.00  # 105.004 → 105.00
        assert df["spot"][0] == 17480.57  # 17480.567 → 17480.57
        assert df["strike"][0] == 17500.01  # 17500.005 → 17500.01
        assert df["iv"][0] == pytest.approx(0.1523)  # NOT rounded — full precision
        assert df["side"][0] == "CE"

    def test_addressing_columns_written(self, tmp_path: Path) -> None:
        # DEC-EXPIRED-OPTIONS-TABLE-001: every series-addressing field is a
        # real, self-describing column on every bar (promoted from the path).
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        df = _read(tmp_path)
        # Final column order is the full 18-column expired-options schema.
        assert df.columns == [
            "exch_id",
            "exchange_segment",
            "security_id",
            "underlying_symbol",
            "expiry_flag",
            "expiry_code",
            "side",
            "strike_offset",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            "strike",
            "spot",
            "iv",
        ]
        assert df["exch_id"][0] == "NSE"
        assert df["exchange_segment"][0] == "NSE_FNO"
        assert df["security_id"][0] == 13
        assert df["security_id"].dtype == pl.Int64
        assert df["underlying_symbol"][0] == "NIFTY"
        assert df["expiry_flag"][0] == "WEEK"
        assert df["expiry_code"][0] == 0
        assert df["expiry_code"].dtype == pl.Int64
        assert df["strike_offset"][0] == "ATM"  # requested selector, distinct from strike

    def test_empty_bars_returns_zero(self, tmp_path: Path) -> None:
        written = _writer(tmp_path).write(partition_key=_PK, coordinate=_COORD, bars=[])
        assert written == 0

    def test_idempotent_repull_writes_nothing(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        bars = [_bar(minute=0), _bar(minute=1)]
        first = w.write(partition_key=_PK, coordinate=_COORD, bars=bars)
        assert first == 2
        second = w.write(partition_key=_PK, coordinate=_COORD, bars=bars)
        assert second == 0  # all dup → no new rows
        assert _read(tmp_path).height == 2

    def test_partial_merge_adds_only_new(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        added = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0), _bar(minute=1)])
        assert added == 1  # minute 0 dup, minute 1 new
        assert _read(tmp_path).height == 2

    def test_ce_and_pe_same_minute_both_kept(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        added = w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(side="CE", minute=0), _bar(side="PE", minute=0)],
        )
        assert added == 2  # distinct side → distinct (side, ts) key
        assert _read(tmp_path).height == 2

    def test_bars_split_across_months(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        added = w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(month=1, minute=0), _bar(month=2, minute=0)],
        )
        assert added == 2
        files = sorted((tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL").glob("*.parquet"))
        assert [f.name for f in files] == ["2021-01.parquet", "2021-02.parquet"]

    def test_none_strike_spot_persist_as_null(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)

        bar = OptionBar(
            timestamp=datetime(2021, 1, 4, 4, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=0,
            open_interest=None,
            side="CE",
            strike=None,
            spot=None,
            iv=None,
        )
        written = w.write(partition_key=_PK, coordinate=_COORD, bars=[bar])
        assert written == 1
        df = _read(tmp_path)
        assert df["strike"][0] is None
        assert df["open_interest"][0] is None


class TestPoisonedPartitionMergeKeepsNewKeys:
    """A physically-duplicated partition must never swallow genuinely-NEW keys.

    Review round 3 (pre-existing writer defect, flagged by the
    DEC-LAKE-STATS-MANIFEST-001 refuter panel): ``added`` was computed against
    the PHYSICAL height of the existing file, so on a pre-uniqueness-era
    poisoned partition (10 physical / 7 distinct ``(side, timestamp)`` keys) a
    merge bringing 2 genuinely NEW keys computed ``9 − 10 = −1``, skipped the
    write entirely, and silently DROPPED the new bars for that pull. The
    baseline is now the DISTINCT key count — a clean partition (physical ==
    distinct) is byte-identical.
    """

    def _poison_physical_dups(self, tmp_path: Path) -> Path:
        """Store 7 distinct keys, then plant 3 physical duplicates (10/7)."""
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=m) for m in range(7)])
        (dest,) = (tmp_path / "options").rglob("*.parquet")
        df = pl.read_parquet(dest)
        pl.concat([df, df[0:3]]).write_parquet(dest)
        assert pl.read_parquet(dest).height == 10  # 10 physical / 7 distinct
        return dest

    def test_new_keys_on_poisoned_partition_are_written(self, tmp_path: Path) -> None:
        """FAILS on the physical baseline: ``added == −1``, write skipped, bars lost."""
        dest = self._poison_physical_dups(tmp_path)
        added = _writer(tmp_path).write(
            partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=7), _bar(minute=8)]
        )
        assert added == 2  # exactly the genuinely-new distinct keys
        stored = pl.read_parquet(dest)
        minutes = sorted(ts.minute for ts in stored["timestamp"].to_list())
        assert 7 in minutes and 8 in minutes  # the new bars are DURABLE
        # The rewrite keep-first-collapsed the physical dups — the SAME rows
        # heal_partition would keep (unique(keep="first", maintain_order=True)).
        assert stored.height == 9
        assert int(stored.n_unique(subset=["side", "timestamp"])) == 9

    def test_pure_repull_on_poisoned_partition_stays_a_no_write(self, tmp_path: Path) -> None:
        """No new keys ⇒ ``added == 0`` (the old arithmetic returned −3) and the
        file is byte-untouched — the merge is not a covert heal (lake_heal owns
        the quarantine-copy + rewrite of a poisoned file)."""
        dest = self._poison_physical_dups(tmp_path)
        before_bytes = dest.read_bytes()
        added = _writer(tmp_path).write(
            partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0), _bar(minute=1)]
        )
        assert added == 0
        assert dest.read_bytes() == before_bytes


class TestAtomicWriteCleanup:
    def test_tmp_file_cleaned_up_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force os.replace to fail AFTER the tmp parquet is written → the cleanup
        # must unlink the orphan tmp so no .tmp leaks. _atomic_write_parquet now
        # delegates to the shared atomic_io helper (DEC-LAKE-WRITE-HALT-001), so the
        # rename happens there — patch os.replace via the module path. A generic OSError
        # (no ENOSPC errno) re-raises as-is (it is NOT escalated to a LakeWriteHalt).
        def _boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _boom)
        w = _writer(tmp_path)
        with pytest.raises(OSError, match="disk full"):
            w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        # No orphan tmp file left behind.
        leftovers = list((tmp_path / "options").rglob("*.tmp"))
        assert leftovers == []

    def test_merge_read_oom_halts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A MemoryError during the merge READ (steady-state re-pull) → LakeWriteHalt.

        The merge read loads the WHOLE existing partition; an OOM there must become a
        graceful LakeWriteHalt pull-wide stop, not a raw worker crash
        (DEC-LAKE-WRITE-HALT-001 — the merge-read guard).
        """
        from brutex.data.errors import LakeWriteHalt

        w = _writer(tmp_path)
        # First write creates the partition (no merge read).
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError("merge read OOM")

        monkeypatch.setattr(pl, "read_parquet", _oom)
        # The SECOND write merges → reads the existing partition → OOM → LakeWriteHalt.
        with pytest.raises(LakeWriteHalt) as exc_info:
            w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=1)])
        assert exc_info.value.reason == "out_of_memory"


class TestPartitionLock:
    """Per-partition write serialisation (DEC-OPTION-PARTITION-LOCK-001).

    The merge-on-write is a read-modify-write, so when the rolling-option fetch is
    parallelised (DEC-DHAN-PARALLEL-INGEST-001) two windows of ONE series can target
    the SAME month partition (a 30-day option pull straddles a month boundary) and a
    naive concurrent ``os.replace`` would drop the loser's bars. A per-PATH lock
    serialises only same-file writers; different partitions never contend. This is
    belt-and-braces below the coarse ``OptionBarSink`` audit lock — the writer is now
    correct on its own.
    """

    def test_same_path_serialises(self, tmp_path: Path) -> None:
        from brutex.data.option_parquet_writer import _PARTITION_LOCKS

        dest = tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL" / "2021-01.parquet"
        # Interned by path: a nested non-blocking acquire of the underlying lock must
        # FAIL while the outer one is held (two OptionParquetWriter instances serialise).
        with _PARTITION_LOCKS.acquire(dest):
            with _PARTITION_LOCKS._guard:  # pyright: ignore[reportPrivateUsage]
                entry = _PARTITION_LOCKS._locks[dest]  # pyright: ignore[reportPrivateUsage]
            nested_acquired = entry.lock.acquire(blocking=False)
            assert nested_acquired is False

    def test_different_paths_do_not_contend(self, tmp_path: Path) -> None:
        from brutex.data.option_parquet_writer import _PARTITION_LOCKS

        a = tmp_path / "options" / "a" / "2021-01.parquet"
        b = tmp_path / "options" / "b" / "2021-02.parquet"
        # Distinct partitions get distinct locks → holding a's lock never blocks b's
        # (the fan-out keeps its parallelism); b is not even interned while a is held.
        with _PARTITION_LOCKS.acquire(a):
            with _PARTITION_LOCKS._guard:  # pyright: ignore[reportPrivateUsage]
                b_entry = _PARTITION_LOCKS._locks.get(b)  # pyright: ignore[reportPrivateUsage]
            assert b_entry is None

    def test_concurrent_same_partition_writes_lose_no_bars(self, tmp_path: Path) -> None:
        """16 threads merge-writing the same month from a barrier keep ALL bars.

        Without the per-partition lock this is a lost-update race (every thread reads
        the empty/partial file, each unions only its own bar, the later rename wins).
        With the lock the merges serialise and the partition holds the UNION of every
        thread's bars regardless of completion order (Rule #7).
        """
        import threading

        w = _writer(tmp_path)
        n_threads = 16
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def _write(minute: int) -> None:
            # Each thread writes a DISTINCT (side, timestamp) into the SAME (year,
            # month) partition — the union must end up with all n_threads rows.
            barrier.wait()  # maximise the race window
            try:
                w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=minute)])
            except Exception as exc:  # surface any worker failure to the assert below
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(m,)) for m in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        df = _read(tmp_path)
        # No lost update: every distinct-minute bar survived the merge.
        assert df.height == n_threads
        minutes = sorted(ts.minute for ts in df["timestamp"].to_list())
        assert minutes == list(range(n_threads))


class TestCorruptExistingPartition:
    """A corrupt existing option partition quarantines its series, never crashes (GAP #1).

    The merge READ of the EXISTING partition was unguarded, so a single corrupt Parquet
    on disk crashed the WHOLE option pull. The read now raises a QUARANTINABLE
    ``OptionWriterError`` naming the bad path, so the spine skips that one contract and
    every OTHER contract still pulls.
    """

    def test_corrupt_existing_partition_raises_option_writer_error(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        # First write creates a real partition.
        written = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        assert written == 1
        (dest,) = (tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL").glob("*.parquet")
        # Overwrite with garbage so the REAL pl.read_parquet raises a PolarsError on merge.
        dest.write_bytes(b"this is not a parquet file at all")
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=1)])

    def test_one_corrupt_series_does_not_block_a_distinct_series(self, tmp_path: Path) -> None:
        """A garbage partition for ONE series quarantines it; a DIFFERENT series still writes.

        The lock + read are PER PATH, so a corrupt partition for series A raises only when
        A is re-written — series B (a different partition key) writes cleanly throughout.
        """
        w = _writer(tmp_path)
        other_pk = "NSE_FNO|13|WEEK|0|ATM|PUT"
        # Series A gets a real partition, then is corrupted on disk.
        written_a = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        assert written_a == 1
        (dest_a,) = (tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL").glob("*.parquet")
        dest_a.write_bytes(b"garbage")
        # Series B (distinct partition) writes cleanly — never blocked by A's corruption.
        written_b = w.write(partition_key=other_pk, coordinate=_COORD, bars=[_bar(side="PE")])
        assert written_b == 1
        # A re-write of the corrupt series A still quarantines (raises), in isolation.
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=2)])
        # B's partition is intact + readable (the quarantine of A did not touch it).
        df_b = _read(tmp_path, segment="NSE_FNO__13__WEEK__0__ATM__PUT")
        assert df_b.height == 1


class TestPathSafety:
    def test_empty_partition_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OptionWriterError, match="non-empty"):
            _writer(tmp_path).write(partition_key="", coordinate=_COORD, bars=[_bar()])

    @pytest.mark.parametrize("bad", ["a/b", "a\\b", ".."])
    def test_unsafe_partition_key_raises(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(OptionWriterError, match="unsafe"):
            _writer(tmp_path).write(partition_key=bad, coordinate=_COORD, bars=[_bar()])

    def test_lake_root_property(self, tmp_path: Path) -> None:
        assert _writer(tmp_path).lake_root == tmp_path


class TestCrossProcessPartitionLock:
    """Cross-process partition write-lock (DEC-LAKE-PARTITION-FLOCK-001).

    The intra-process ``threading.Lock`` only serialises threads in ONE
    interpreter; a concurrent CLI fetch + a dashboard pull sharing the lake are
    SEPARATE processes and would still lost-update the same option month
    partition. The advisory ``flock`` must be HELD across the read-modify-write.
    """

    def test_flock_is_held_across_the_merge_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The partition flock is held while the writer reads the existing partition.

        Regression for the silent cross-process lost-update: a separate fd (≈ another
        process) is probed DURING the merge read and must find the lock HELD. WITHOUT
        the cross-process flock the probe would succeed and this assertion fails.
        """
        w = _writer(tmp_path)
        # First write creates the partition so the SECOND write takes the merge
        # (read-modify-write) path that calls ``pl.read_parquet(dest)``.
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])

        observed: dict[str, bool] = {}
        original_read = pl.read_parquet

        def _spy_read(source: object, *args: object, **kwargs: object) -> pl.DataFrame:
            observed["held_during_read"] = _partition_lock_is_held(
                partition_lock_path(tmp_path, Path(str(source)))
            )
            return original_read(source, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pl, "read_parquet", _spy_read)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=1)])

        assert observed["held_during_read"] is True
        # And released afterwards (a fresh acquire succeeds) — no leaked lock.
        (only,) = (tmp_path / "options").rglob("*.parquet")
        assert _partition_lock_is_held(partition_lock_path(tmp_path, only)) is False


class TestHealPartition:
    """The public options-tree dup heal (DEC-LAKE-HEAL-001).

    The #1139 machinery never protected ``options/`` — this is the healer's
    ``(side, timestamp)`` keep-first entry, its OWN lock stack + guard.
    """

    def _poison(self, tmp_path: Path) -> Path:
        """Plant a differing LATER duplicate ``(side, timestamp)`` row.

        The writer's unique-merge refuses duplicates, so poison is planted by
        re-rendering the stored frame with a value-drifted copy of row 0
        appended (the #1067 raw-plant convention, polars flavoured).
        """
        w = _writer(tmp_path)
        w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(side="CE", minute=0), _bar(side="PE", minute=0)],
        )
        (dest,) = (tmp_path / "options").rglob("*.parquet")
        df = pl.read_parquet(dest)
        drifted = df[0:1].with_columns((pl.col("close") + 999.0).alias("close"))
        pl.concat([df, drifted]).write_parquet(dest)
        return dest

    def test_heal_partition_dedups_keep_first(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: no options-tree heal entry exists — a
        poisoned ``(side, timestamp)`` file persisted forever."""
        dest = self._poison(tmp_path)
        original_bytes = dest.read_bytes()
        quarantine_to = tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        result = _writer(tmp_path).heal_partition(dest, quarantine_to=quarantine_to)
        assert result.changed is True
        assert result.kept == 2
        assert result.dropped_duplicates == 1
        assert result.quarantined_original == quarantine_to
        assert quarantine_to.read_bytes() == original_bytes  # restorable original
        healed = pl.read_parquet(dest)
        assert healed.height == 2
        ce = healed.filter(pl.col("side") == "CE")
        assert ce["close"].to_list() == [105.0]  # the FIRST physical row won

    def test_heal_noop_is_byte_identical(self, tmp_path: Path) -> None:
        """A clean file (incl. the same ts across CE/PE sides — NOT a dup) is
        left byte-untouched; no quarantine copy is made (idempotence oracle)."""
        w = _writer(tmp_path)
        w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(side="CE", minute=0), _bar(side="PE", minute=0)],
        )
        (dest,) = (tmp_path / "options").rglob("*.parquet")
        before = dest.read_bytes()
        quarantine_to = tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        result = w.heal_partition(dest, quarantine_to=quarantine_to)
        assert result.changed is False
        assert result.kept == 2
        assert result.dropped_duplicates == 0
        assert result.quarantined_original is None
        assert dest.read_bytes() == before
        assert not quarantine_to.exists()

    def test_heal_missing_quarantine_dest_refused(self, tmp_path: Path) -> None:
        """A changing heal with no quarantine destination is REFUSED, file untouched."""
        dest = self._poison(tmp_path)
        poisoned = dest.read_bytes()
        with pytest.raises(OptionWriterError, match="quarantine destination"):
            _writer(tmp_path).heal_partition(dest)
        assert dest.read_bytes() == poisoned

    def test_heal_corrupt_file_raises_quarantinable_error(self, tmp_path: Path) -> None:
        """A garbage/vanished file raises the quarantinable OptionWriterError
        (the healer's per-finding isolation counts it; batch continues)."""
        dest = self._poison(tmp_path)
        dest.write_bytes(b"not a parquet file at all")
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            _writer(tmp_path).heal_partition(
                dest, quarantine_to=tmp_path / ".quarantine" / "x.parquet"
            )
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            _writer(tmp_path).heal_partition(
                tmp_path / "options" / "missing" / "2021-01.parquet",
                quarantine_to=tmp_path / ".quarantine" / "y.parquet",
            )

    def test_heal_holds_partition_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross-process flock is held while the heal reads the partition."""
        dest = self._poison(tmp_path)
        observed: dict[str, bool] = {}
        original_read = pl.read_parquet

        def _spy_read(source: object, *args: object, **kwargs: object) -> pl.DataFrame:
            observed["held_during_read"] = _partition_lock_is_held(
                partition_lock_path(tmp_path, dest)
            )
            return original_read(source, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pl, "read_parquet", _spy_read)
        result = _writer(tmp_path).heal_partition(
            dest, quarantine_to=tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        )
        assert result.changed is True
        assert observed["held_during_read"] is True
        assert _partition_lock_is_held(partition_lock_path(tmp_path, dest)) is False

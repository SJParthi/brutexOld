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
from structlog.testing import capture_logs


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


# ── heal_partition remove_keys (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001) ──────────


def _only_partition(tmp_path: Path) -> Path:
    (dest,) = (tmp_path / "options").rglob("*.parquet")
    return dest


def _plant_naive_option_raw(dest: Path, rows: list[tuple[str, datetime]]) -> None:
    """A hand-planted options file whose ``timestamp`` column is tz-NAIVE."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "side": pa.array([side for side, _ in rows], type=pa.string()),
            "timestamp": pa.array([ts for _, ts in rows], type=pa.timestamp("us")),  # NO tz
        }
    )
    pq.write_table(table, dest)


class TestHealPartitionRemoveKeys:
    """Keyed row removal — the foreign-row reassignment hook."""

    def _seed(self, tmp_path: Path) -> Path:
        w = _writer(tmp_path)
        w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(side="CE", minute=0), _bar(side="PE", minute=0), _bar(side="CE", minute=1)],
        )
        return _only_partition(tmp_path)

    def test_heal_remove_keys_drops_only_named_keys(self, tmp_path: Path) -> None:
        dest = self._seed(tmp_path)
        before = pl.read_parquet(dest)
        ce0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        quarantine_to = tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        result = _writer(tmp_path).heal_partition(
            dest,
            remove_keys={
                ("CE", ce0),  # present — dropped
                ("CE", datetime(2099, 1, 1, tzinfo=UTC)),  # absent — silently ignored
            },
            quarantine_to=quarantine_to,
        )
        assert result.changed is True
        assert result.dropped_removed == 1
        assert result.dropped_duplicates == 0
        assert result.kept == 2
        healed = pl.read_parquet(dest)
        # The other-side same-ts row (PE @ ce0) is KEPT — the key includes side —
        # and every kept row's FULL column tuple is byte-value-identical to the
        # original (STRICTLY row-removing, never value-editing).
        expected = before.filter(~((pl.col("side") == "CE") & (pl.col("timestamp") == ce0))).sort(
            ["side", "timestamp"]
        )
        assert healed.rows() == expected.rows()

    def test_heal_remove_keys_plus_dup_accounting(self, tmp_path: Path) -> None:
        """A removed key ALSO physically duplicated: N−1 dup-dropped, 1 removed."""
        dest = self._seed(tmp_path)
        df = pl.read_parquet(dest)
        ce0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        dup_row = df.filter((pl.col("side") == "CE") & (pl.col("timestamp") == ce0))
        pl.concat([df, dup_row, dup_row]).write_parquet(dest)  # 3 physical copies
        result = _writer(tmp_path).heal_partition(
            dest,
            remove_keys={("CE", ce0)},
            quarantine_to=tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path),
        )
        assert result.changed is True
        assert result.dropped_duplicates == 2  # N−1 physical copies collapsed
        assert result.dropped_removed == 1  # the keep-first survivor removed
        assert result.kept == 2

    def test_heal_remove_all_rows_quarantine_moves_file(self, tmp_path: Path) -> None:
        """Wholly-removed: unlink + the emptied series dir pruned; kept=0."""
        dest = self._seed(tmp_path)
        original = dest.read_bytes()
        keys = {
            (side, ts)
            for side, ts in zip(
                pl.read_parquet(dest)["side"].to_list(),
                pl.read_parquet(dest)["timestamp"].to_list(),
                strict=True,
            )
        }
        quarantine_to = tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        result = _writer(tmp_path).heal_partition(
            dest, remove_keys=keys, quarantine_to=quarantine_to
        )
        assert result.changed is True
        assert result.kept == 0
        assert result.dropped_removed == len(keys)
        assert result.quarantined_original == quarantine_to
        assert quarantine_to.read_bytes() == original
        assert not dest.exists()
        assert not dest.parent.exists()  # the emptied series dir was pruned

    def test_heal_remove_all_rows_sibling_keeps_series_dir(self, tmp_path: Path) -> None:
        """A sibling month file keeps the series dir (the suppressed-rmdir arm)."""
        w = _writer(tmp_path)
        w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(month=1, minute=0), _bar(month=2, minute=0)],
        )
        series_dir = tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL"
        jan = series_dir / "2021-01.parquet"
        result = w.heal_partition(
            jan,
            remove_keys={("CE", datetime(2021, 1, 4, 4, 0, tzinfo=UTC))},
            quarantine_to=tmp_path / ".quarantine" / "ts" / jan.relative_to(tmp_path),
        )
        assert result.changed is True and result.kept == 0
        assert not jan.exists()
        assert series_dir.exists()  # the February sibling keeps the dir alive
        assert (series_dir / "2021-02.parquet").exists()

    def test_heal_remove_keys_noop_is_byte_identical(self, tmp_path: Path) -> None:
        """Absent keys only ⇒ ``changed=False``, dest byte-untouched, no quarantine."""
        dest = self._seed(tmp_path)
        before = dest.read_bytes()
        quarantine_to = tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path)
        result = _writer(tmp_path).heal_partition(
            dest,
            remove_keys={("CE", datetime(2099, 1, 1, tzinfo=UTC))},
            quarantine_to=quarantine_to,
        )
        assert result.changed is False
        assert result.dropped_removed == 0
        assert result.kept == 3
        assert dest.read_bytes() == before
        assert not quarantine_to.exists()

    def test_heal_remove_keys_requires_quarantine(self, tmp_path: Path) -> None:
        dest = self._seed(tmp_path)
        before = dest.read_bytes()
        with pytest.raises(OptionWriterError, match="quarantine destination"):
            _writer(tmp_path).heal_partition(
                dest, remove_keys={("CE", datetime(2021, 1, 4, 4, 0, tzinfo=UTC))}
            )
        assert dest.read_bytes() == before

    def test_heal_remove_keys_matches_naive_timestamp_column(self, tmp_path: Path) -> None:
        """RAW stored-value membership heals a hand-planted NAIVE column.

        A tz-cast anti-join would raise on the naive-vs-aware dtype mismatch
        and leave the file permanently unhealable; the raw boolean mask
        matches the stored values exactly.
        """
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        naive0 = datetime(2021, 1, 4, 4, 0)  # NO tzinfo
        naive1 = datetime(2021, 1, 4, 4, 1)
        _plant_naive_option_raw(dest, [("CE", naive0), ("CE", naive1)])
        result = _writer(tmp_path).heal_partition(
            dest,
            remove_keys={("CE", naive0)},
            quarantine_to=tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path),
        )
        assert result.changed is True
        assert result.dropped_removed == 1
        healed = pl.read_parquet(dest)
        assert healed["timestamp"].to_list() == [naive1]

    def test_heal_remove_null_side_key_matches_verbatim(self, tmp_path: Path) -> None:
        """A hand-planted NULL side rides the key tuple verbatim (M15)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        pq.write_table(
            pa.table(
                {
                    "side": pa.array([None, "CE"], type=pa.string()),
                    "timestamp": pa.array([ts0, ts1], type=pa.timestamp("us", tz="UTC")),
                }
            ),
            dest,
        )
        result = _writer(tmp_path).heal_partition(
            dest,
            remove_keys={(None, ts0)},
            quarantine_to=tmp_path / ".quarantine" / "ts" / dest.relative_to(tmp_path),
        )
        assert result.changed is True
        assert result.dropped_removed == 1
        assert pl.read_parquet(dest)["side"].to_list() == ["CE"]

    def test_heal_remove_keys_oom_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An allocation failure inside the guard is a LakeWriteHalt, never raw."""
        from brutex.data.errors import LakeWriteHalt

        dest = self._seed(tmp_path)

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError("heal read OOM")

        monkeypatch.setattr(pl, "read_parquet", _oom)
        with pytest.raises(LakeWriteHalt) as exc_info:
            _writer(tmp_path).heal_partition(
                dest,
                remove_keys={("CE", datetime(2021, 1, 4, 4, 0, tzinfo=UTC))},
                quarantine_to=tmp_path / ".quarantine" / "x.parquet",
            )
        assert exc_info.value.reason == "out_of_memory"


class TestReadPartitionFrame:
    """The healer's consistent RAW source read (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001)."""

    def test_raw_physical_rows_preserved(self, tmp_path: Path) -> None:
        """A planted physical duplicate SURVIVES the read (truthful raw read)."""
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        dest = _only_partition(tmp_path)
        df = pl.read_parquet(dest)
        pl.concat([df, df[0:1]]).write_parquet(dest)  # plant a physical dup
        frame = w.read_partition_frame(dest)
        assert frame.height == 2  # NOT keep-first collapsed

    def test_corrupt_and_missing_raise_option_writer_error(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        dest = _only_partition(tmp_path)
        dest.write_bytes(b"not a parquet file at all")
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            w.read_partition_frame(dest)
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            w.read_partition_frame(tmp_path / "options" / "missing" / "2021-01.parquet")

    def test_read_holds_partition_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        dest = _only_partition(tmp_path)
        observed: dict[str, bool] = {}
        original_read = pl.read_parquet

        def _spy_read(source: object, *args: object, **kwargs: object) -> pl.DataFrame:
            observed["held_during_read"] = _partition_lock_is_held(
                partition_lock_path(tmp_path, dest)
            )
            return original_read(source, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pl, "read_parquet", _spy_read)
        frame = w.read_partition_frame(dest)
        assert frame.height == 1
        assert observed["held_during_read"] is True
        assert _partition_lock_is_held(partition_lock_path(tmp_path, dest)) is False


class TestMergePartitionFrame:
    """The reassignment heal's write-home entry — keep-first at the destination."""

    def _frame(self, rows: list[tuple[str, datetime, float]]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "side": pl.Series([side for side, _, _ in rows], dtype=pl.Utf8),
                "timestamp": pl.Series([ts for _, ts, _ in rows], dtype=pl.Datetime("us", "UTC")),
                "close": pl.Series([close for _, _, close in rows], dtype=pl.Float64),
            }
        )

    def test_fresh_destination_created_uniqued_sorted(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        frame = self._frame([("PE", ts1, 3.0), ("CE", ts0, 1.0), ("CE", ts0, 999.0)])
        added = w.merge_partition_frame(dest, frame)
        assert added == 2  # the in-frame dup collapsed keep-first
        stored = pl.read_parquet(dest)
        assert stored["side"].to_list() == ["CE", "PE"]  # sorted (side, ts)
        assert stored["close"].to_list() == [1.0, 3.0]  # FIRST physical row won

    def test_existing_destination_keep_first_never_restates(self, tmp_path: Path) -> None:
        """A value-conflicting same-key row does NOT restate the stored value."""
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        added = w.merge_partition_frame(dest, self._frame([("CE", ts0, 999.0), ("PE", ts1, 2.0)]))
        assert added == 1  # the conflicting key added 0; PE added
        stored = pl.read_parquet(dest).sort(["side", "timestamp"])
        assert stored["close"].to_list() == [1.0, 2.0]  # canonical value KEPT

    def test_pure_remerge_returns_zero_and_rewrites_nothing(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        before_bytes = dest.read_bytes()
        before_mtime = dest.stat().st_mtime_ns
        added = w.merge_partition_frame(dest, self._frame([("CE", ts0, 999.0)]))
        assert added == 0
        assert dest.read_bytes() == before_bytes
        assert dest.stat().st_mtime_ns == before_mtime  # not even rewritten

    def test_corrupt_destination_raises_option_writer_error(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"garbage")
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        with pytest.raises(OptionWriterError, match="corrupt existing partition"):
            w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))

    def test_merge_holds_partition_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross-process lock is HELD during the union's read-modify-write."""
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        observed: dict[str, bool] = {}
        original_read = pl.read_parquet

        def _spy_read(source: object, *args: object, **kwargs: object) -> pl.DataFrame:
            observed["held_during_read"] = _partition_lock_is_held(
                partition_lock_path(tmp_path, dest)
            )
            return original_read(source, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pl, "read_parquet", _spy_read)
        added = w.merge_partition_frame(dest, self._frame([("PE", ts1, 2.0)]))
        assert added == 1
        assert observed["held_during_read"] is True
        assert _partition_lock_is_held(partition_lock_path(tmp_path, dest)) is False

    def test_dup_poisoned_destination_never_cancels_new_keys(self, tmp_path: Path) -> None:
        """BLOCKER regression (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 hostile review):
        ``added`` is baselined on the destination's UNIQUE stored keys, so a
        physical dup in the destination can never cancel a genuinely-new merged
        key (the pre-fix physical-row baseline read ``added == 0``, skipped the
        rewrite, and a reassignment heal's moved key landed NOWHERE while its
        source copy was removed — silent row loss)."""
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        # Poison the destination with a physical dup copy (raw rewrite — the
        # writer itself refuses to store duplicates, the #1067 convention).
        stored = pl.read_parquet(dest)
        pl.concat([stored, stored[0:1]]).write_parquet(dest)
        with capture_logs() as logs:
            added = w.merge_partition_frame(dest, self._frame([("PE", ts1, 2.0)]))
        assert added == 1  # NOT cancelled by the dup collapse
        after = pl.read_parquet(dest).sort(["side", "timestamp"])
        assert after["side"].to_list() == ["CE", "PE"]  # deduped AND the new key landed
        assert after["close"].to_list() == [1.0, 2.0]
        healed = [e for e in logs if e["event"] == "lake.partition.dedup_healed"]
        assert len(healed) == 1  # the collapse is OBSERVABLE (the bars twin's event)
        assert healed[0]["partition"] == str(dest)
        assert healed[0]["kept"] == 1
        assert healed[0]["dropped"] == 1

    def test_dup_poisoned_destination_forced_rewrite_on_zero_added(self, tmp_path: Path) -> None:
        """``added == 0`` but the poisoned destination is STILL rewritten
        keep-first-deduped (the bars ``heal_dropped`` forced-rewrite mirror) —
        a poisoned destination self-heals on touch instead of silently
        returning the poisoned bytes forever."""
        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"
        w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        stored = pl.read_parquet(dest)
        pl.concat([stored, stored[0:1]]).write_parquet(dest)
        with capture_logs() as logs:
            added = w.merge_partition_frame(dest, self._frame([("CE", ts0, 999.0)]))
        assert added == 0  # every incoming key already stored
        after = pl.read_parquet(dest)
        assert after.height == 1  # the physical dup pair collapsed on touch
        assert after["close"].to_list() == [1.0]  # FIRST stored row won, never restated
        healed = [e for e in logs if e["event"] == "lake.partition.dedup_healed"]
        assert len(healed) == 1
        assert healed[0]["kept"] == 1
        assert healed[0]["dropped"] == 1

    def test_merge_enospc_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full disk during the union write is a graceful LakeWriteHalt."""
        import errno

        from brutex.data.errors import LakeWriteHalt

        w = _writer(tmp_path)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        dest = tmp_path / "options" / "SERIES" / "2021-01.parquet"

        def _enospc(src: object, dst: object) -> None:
            raise OSError(errno.ENOSPC, "no space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        with pytest.raises(LakeWriteHalt) as exc_info:
            w.merge_partition_frame(dest, self._frame([("CE", ts0, 1.0)]))
        assert exc_info.value.reason == "disk_full"


class TestWriteByteParityAfterRefactor:
    """G10: the `_merge_frame_locked` extraction renders byte-identical files."""

    def test_write_byte_parity_after_refactor(self, tmp_path: Path) -> None:
        from brutex.data.option_parquet_writer import _render_df

        jan_bars = [_bar(month=1, minute=1), _bar(month=1, minute=0), _bar(side="PE", minute=0)]
        feb_bars = [_bar(month=2, minute=0)]
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[*jan_bars, *feb_bars])
        # A SECOND overlapping write exercises the MERGE arm's parity too.
        second = [_bar(month=2, minute=0), _bar(month=2, minute=1)]
        w.write(partition_key=_PK, coordinate=_COORD, bars=second)
        series = tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL"
        # Independent expectation: the SAME documented union expression applied
        # inline (version-robust — both sides render in-run, no stored bytes).
        expected_jan = (
            _render_df(_COORD, jan_bars)
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        expected_feb_first = (
            _render_df(_COORD, feb_bars)
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        expected_feb = (
            pl.concat([expected_feb_first, _render_df(_COORD, second)])
            .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
            .sort(["side", "timestamp"])
        )
        scratch_jan = tmp_path / "expected-jan.parquet"
        scratch_feb = tmp_path / "expected-feb.parquet"
        expected_jan.write_parquet(scratch_jan)
        expected_feb.write_parquet(scratch_feb)
        assert (series / "2021-01.parquet").read_bytes() == scratch_jan.read_bytes()
        assert (series / "2021-02.parquet").read_bytes() == scratch_feb.read_bytes()


class TestMutationContainment:
    """The round-2 mutation-boundary symlink rail (``_assert_options_containment``).

    Hostile-review round-2 MAJOR (refuter-confirmed): the scanner's scan-time
    ``is_symlink()`` probe was the ONLY control, defeated by (Vector 1) a
    symlinked ``options/`` ROOT — its series dirs are real, so the per-series
    probe never fired — and (Vector 2) a series dir swapped to a symlink
    between scan and heal (TOCTOU). Both let the default AUTO-ON heal
    create/unlink parquet files OUTSIDE the lake root. The rail runs at heal
    time on the RESOLVED destination, inside both heal-side writer entries.
    """

    def _frame(self, rows: list[tuple[str, datetime, float]]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "side": pl.Series([side for side, _, _ in rows], dtype=pl.Utf8),
                "timestamp": pl.Series([ts for _, ts, _ in rows], dtype=pl.Datetime("us", "UTC")),
                "close": pl.Series([close for _, _, close in rows], dtype=pl.Float64),
            }
        )

    def _outside_series(self, tmp_path: Path) -> Path:
        """A REAL series tree OUTSIDE the victim lake, seeded via the writer."""
        outside_lake = tmp_path / "outside-lake"
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        OptionParquetWriter(lake_root=outside_lake).merge_partition_frame(
            outside_lake / "options" / "SERIES" / "2021-01.parquet",
            self._frame([("CE", ts0, 1.0)]),
        )
        return outside_lake / "options" / "SERIES"

    def test_heal_refuses_symlinked_series_dir(self, tmp_path: Path) -> None:
        """Vector 2 (scan→heal TOCTOU): ``heal_partition`` through a series dir
        that is (now) a symlink is REFUSED before ANY read/unlink/rewrite —
        the outside target file is byte-untouched. FAILS pre-fix: the heal
        followed the link and mutated the outside file."""
        outside_series = self._outside_series(tmp_path)
        outside_file = outside_series / "2021-01.parquet"
        outside_bytes = outside_file.read_bytes()
        lake = tmp_path / "lake"
        (lake / "options").mkdir(parents=True)
        (lake / "options" / "SERIES").symlink_to(outside_series, target_is_directory=True)
        dest = lake / "options" / "SERIES" / "2021-01.parquet"
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            OptionParquetWriter(lake_root=lake).heal_partition(
                dest, quarantine_to=lake / ".quarantine" / "ts" / "2021-01.parquet"
            )
        assert outside_file.read_bytes() == outside_bytes
        assert not (lake / ".quarantine").exists()

    def test_merge_refuses_symlinked_series_dir(self, tmp_path: Path) -> None:
        """Vector 2, the write-home leg: ``merge_partition_frame`` must not
        CREATE a file outside the lake through a swapped-in symlink."""
        outside_series = self._outside_series(tmp_path)
        lake = tmp_path / "lake"
        (lake / "options").mkdir(parents=True)
        (lake / "options" / "SERIES").symlink_to(outside_series, target_is_directory=True)
        ts1 = datetime(2021, 2, 4, 4, 0, tzinfo=UTC)
        dest = lake / "options" / "SERIES" / "2021-02.parquet"
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            OptionParquetWriter(lake_root=lake).merge_partition_frame(
                dest, self._frame([("CE", ts1, 2.0)])
            )
        assert not (outside_series / "2021-02.parquet").exists()  # nothing created outside

    def test_merge_refuses_symlinked_options_root(self, tmp_path: Path) -> None:
        """Vector 1: a symlinked ``options/`` ROOT re-anchors nothing — the
        ``options`` path component is deliberately NOT symlink-resolved, so
        the resolved destination falls outside ``<resolved lake>/options``
        and the mutation is refused."""
        outside_series = self._outside_series(tmp_path)
        lake = tmp_path / "lake"
        lake.mkdir()
        (lake / "options").symlink_to(outside_series.parent, target_is_directory=True)
        ts1 = datetime(2021, 2, 4, 4, 0, tzinfo=UTC)
        dest = lake / "options" / "SERIES" / "2021-02.parquet"
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            OptionParquetWriter(lake_root=lake).merge_partition_frame(
                dest, self._frame([("CE", ts1, 2.0)])
            )
        assert not (outside_series / "2021-02.parquet").exists()

    def test_lake_root_reached_via_symlink_still_heals(self, tmp_path: Path) -> None:
        """A lake ROOT addressed through a symlink (a legitimate mount shape,
        e.g. the §8 external SSD behind a stable alias) PASSES: both sides of
        the containment check resolve consistently — no false-positive
        regression for real deployments."""
        real = tmp_path / "real-lake"
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        ts1 = datetime(2021, 1, 4, 4, 1, tzinfo=UTC)
        OptionParquetWriter(lake_root=real).merge_partition_frame(
            real / "options" / "SERIES" / "2021-01.parquet", self._frame([("CE", ts0, 1.0)])
        )
        alias = tmp_path / "alias-lake"
        alias.symlink_to(real, target_is_directory=True)
        w = OptionParquetWriter(lake_root=alias)
        dest = alias / "options" / "SERIES" / "2021-01.parquet"
        added = w.merge_partition_frame(dest, self._frame([("PE", ts1, 2.0)]))
        assert added == 1  # containment passed; the merge landed
        result = w.heal_partition(
            dest, quarantine_to=alias / ".quarantine" / "ts" / "2021-01.parquet"
        )
        assert result.changed is False  # clean file — containment passed, no-op heal
        stored = pl.read_parquet(real / "options" / "SERIES" / "2021-01.parquet")
        assert stored["side"].to_list() == ["CE", "PE"]

    def test_read_refuses_symlinked_partition_file(self, tmp_path: Path) -> None:
        """Round-4 MINOR (refuter-confirmed): the healer's source READ must not
        follow a symlinked partition file out of the lake — pre-fix
        ``read_partition_frame`` had NO containment check, so the reassignment
        heal read the OUTSIDE rows and its copy-forward leg imported them into
        an in-lake canonical file (that dest's parent is a REAL series dir, so
        the write-leg rail passed). Refused at the read instead; the outside
        file is byte-untouched."""
        outside_series = self._outside_series(tmp_path)
        outside_file = outside_series / "2021-01.parquet"
        outside_bytes = outside_file.read_bytes()
        lake = tmp_path / "lake"
        (lake / "options" / "SERIES").mkdir(parents=True)
        src = lake / "options" / "SERIES" / "2021-01.parquet"
        src.symlink_to(outside_file)
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            OptionParquetWriter(lake_root=lake).read_partition_frame(src)
        assert outside_file.read_bytes() == outside_bytes

    def test_heal_refuses_symlinked_partition_file(self, tmp_path: Path) -> None:
        """A file-level symlink resolves to its target's parent — refused too
        (``heal_partition`` unlinks/rewrites ``dest`` itself)."""
        outside_series = self._outside_series(tmp_path)
        outside_file = outside_series / "2021-01.parquet"
        outside_bytes = outside_file.read_bytes()
        lake = tmp_path / "lake"
        (lake / "options" / "SERIES").mkdir(parents=True)
        dest = lake / "options" / "SERIES" / "2021-01.parquet"
        dest.symlink_to(outside_file)
        with pytest.raises(OptionWriterError, match="outside the options tree"):
            OptionParquetWriter(lake_root=lake).heal_partition(
                dest, quarantine_to=lake / ".quarantine" / "ts" / "2021-01.parquet"
            )
        assert outside_file.read_bytes() == outside_bytes

    def test_symlink_loop_dest_refused_as_writer_error(self, tmp_path: Path) -> None:
        """Round-5 MAJOR (refuter-confirmed): a symlink-LOOP ``dest`` made
        ``_assert_options_containment``'s ``dest.resolve()`` raise a bare
        ``RuntimeError('Symlink loop from …')`` on Python ≤ 3.12 —
        ``RuntimeError`` is NOT in the healer's per-finding isolation set,
        so the containment check itself became a batch-aborting escape (the
        same isolation-escape class the round-3 zero-row fold eliminated).
        All THREE heal-side entries must refuse with the quarantinable
        :class:`OptionWriterError` instead; nothing is created or mutated."""
        lake = tmp_path / "lake"
        series = lake / "options" / "SERIES"
        series.mkdir(parents=True)
        loop = series / "2021-01.parquet"
        loop.symlink_to(loop)  # self-referential symlink loop
        writer = OptionParquetWriter(lake_root=lake)
        ts0 = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
        with pytest.raises(OptionWriterError, match="cannot resolve"):
            writer.merge_partition_frame(loop, self._frame([("CE", ts0, 1.0)]))
        with pytest.raises(OptionWriterError, match="cannot resolve"):
            writer.heal_partition(
                loop, quarantine_to=lake / ".quarantine" / "ts" / "2021-01.parquet"
            )
        with pytest.raises(OptionWriterError, match="cannot resolve"):
            writer.read_partition_frame(loop)
        assert not (lake / ".quarantine").exists()  # refused before any copy
        assert loop.is_symlink()  # the loop itself is left untouched

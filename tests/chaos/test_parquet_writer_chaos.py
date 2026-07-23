"""Chaos tests for the SPOT :class:`~brutex.data.parquet_writer.ParquetWriter`
(DEC-CHAOS-RACE-001).

Companion to ``tests/chaos/test_audit_chain_chaos.py`` (audit log) and
``tests/chaos/test_sweep_resume_chaos.py`` (sweep engine). These prove the spot
lake's two hard write-durability invariants under chaotic schedules (CLAUDE.md
Golden Rule #7 — idempotence + crash safety):

1. **A multi-writer race on ONE partition never loses an update.** When many
   threads merge-write the SAME ``(exchange, segment, symbol, interval, year,
   month)`` partition concurrently (the real per-window spot fan-out, where two
   adjacent 90-day windows straddle a month boundary), the per-partition lock
   (``DEC-DHAN-PARALLEL-WINDOWS-001``) serialises the read-modify-write so the
   final file is the UNION of every writer's bars — no thread's bars are silently
   dropped by a lost ``os.replace``, and the stored file is always valid Parquet.

2. **A mid-write crash / ENOSPC leaves NO partial or corrupt partition.** The
   atomic tmp+fsync+``os.replace`` write means a fault at ANY point (fsync raising
   ``ENOSPC``, or a "crash" at the ``os.replace`` boundary) leaves the partition
   either ABSENT (a first write that never landed) or EXACTLY the previously
   committed bytes (a re-write that rolled back) — never a half-written file — and
   never leaks a ``.tmp`` sidecar.

``CHAOS_ROUNDS`` / ``CHAOS_WRITERS`` scale the run. Marked ``chaos`` so the
default unit suite stays fast. Whitebox: drives the private ``_atomic_write`` /
``_PARTITION_LOCKS`` fault points, so the file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import errno
import os
import random
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

pytestmark = pytest.mark.chaos


@pytest.fixture(autouse=True)
def _pin_python_atomic_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Python atomic-write arm for every chaos row.

    These tests inject faults by monkeypatching ``os.replace`` / ``os.fsync``.
    The default-ON ``BRUTEX_LAKE_RUST_LOCKS`` seam (DEC-RUST-DEFAULT-ON-WAVE-001)
    routes the chmod->fsync->replace commit through the ``brutex-lake`` Rust twin,
    which BYPASSES the Python monkeypatch in a wheel-bearing lane — so the injected
    ENOSPC / crash never lands and ``pytest.raises(...)`` reads ``DID NOT RAISE``.
    Pin the explicit ``"0"`` silent kill switch: the Python arm is bit-identical to
    the Rust arm (16-call burn-in guarantee), so write behaviour is unchanged, and
    the Rust arm's own OSError->LakeWriteHalt propagation is covered by the
    dedicated lake-locks parity lane.
    """
    monkeypatch.setenv("BRUTEX_LAKE_RUST_LOCKS", "0")


#: Rounds of randomized crash-point injection (overridable for a soak).
_ROUNDS = int(os.environ.get("CHAOS_ROUNDS", "40"))
#: Concurrent writer threads racing one partition.
_WRITERS = int(os.environ.get("CHAOS_WRITERS", "8"))

#: All bars land in the 2021-01 partition (44 640 minutes fit in January).
_BASE = datetime(2021, 1, 1, 0, 0, tzinfo=UTC)

# Fixed partition coordinates shared by every writer in a test.
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"
_INTERVAL = "1minute"


def _bar(idx: int) -> CandleBar:
    """Build the deterministic bar for index *idx* (a distinct minute in 2021-01).

    Price is a stable function of *idx* so two writers that (correctly) never
    share an index never manufacture a spurious :class:`BarConflict`.
    """
    price = 100.0 + (idx % 50) * 0.25
    return CandleBar(
        timestamp=_BASE + timedelta(minutes=idx),
        open=Decimal(str(round(price, 2))),
        high=Decimal(str(round(price + 0.5, 2))),
        low=Decimal(str(round(price - 0.5, 2))),
        close=Decimal(str(round(price + 0.25, 2))),
        volume=10 + idx % 7,
        open_interest=None,
    )


def _write(writer: ParquetWriter, bars: list[CandleBar]) -> None:
    writer.write(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        bars=bars,
    )


def _partition_file(lake_root: Path) -> Path:
    """Locate the single partition file under *lake_root* (there is exactly one)."""
    files = list(lake_root.rglob("*.parquet"))
    assert len(files) == 1, f"expected exactly one partition file, found {files}"
    return files[0]


def _stray_tmp_files(lake_root: Path) -> list[Path]:
    """Return any leftover atomic-write ``*.tmp`` sidecars (must always be empty)."""
    return list(lake_root.rglob(".brutex-write-*.parquet.tmp"))


# ────── invariant 1: concurrent same-partition writers never lose an update ──────


@pytest.mark.parametrize("seed", [1, 99])
def test_concurrent_same_partition_writers_union_no_lost_update(tmp_path: Path, seed: int) -> None:
    """N threads merge-write DISJOINT bar sets to one partition concurrently → the
    final file is the exact UNION of all writers' bars (no lost ``os.replace``).

    Each thread owns a disjoint index range (the production fan-out's per-window
    bars), so the only way a bar can be missing from the result is a lost-update
    race — which the per-partition lock forbids. We assert the row count equals the
    grand total, every timestamp is present, the file is valid Parquet, and no
    thread raised.
    """
    pl = pytest.importorskip("polars")
    rng = random.Random(seed)
    lake_root = tmp_path / "lake"

    per_writer = 40
    # Disjoint, shuffled index ranges so the threads interleave unpredictably.
    ranges = [list(range(w * per_writer, (w + 1) * per_writer)) for w in range(_WRITERS)]
    for r in ranges:
        rng.shuffle(r)
    total = _WRITERS * per_writer

    errors: list[BaseException] = []
    barrier = threading.Barrier(_WRITERS)

    def worker(indices: list[int]) -> None:
        # One writer instance per worker (the production fan-out pattern); the
        # per-PATH lock is shared across instances, so the merge is serialised.
        writer = ParquetWriter(lake_root=lake_root)
        bars = [_bar(i) for i in indices]
        try:
            barrier.wait(timeout=30)  # release all writers together → maximal contention
            _write(writer, bars)
        except BaseException as exc:  # surface ANY thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(r,)) for r in ranges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent writer raised: {errors[:1]}"
    assert not _stray_tmp_files(lake_root), "a stray .tmp sidecar leaked"

    df = pl.read_parquet(_partition_file(lake_root))
    assert df.height == total, f"lost update: {df.height} rows, expected {total}"
    stored_minutes = {
        int((ts.replace(tzinfo=UTC) - _BASE).total_seconds() // 60)
        for ts in df["timestamp"].to_list()
    }
    assert stored_minutes == set(range(total)), "a writer's bars went missing"


def test_repeated_concurrent_races_are_stable(tmp_path: Path) -> None:
    """Re-run the race many times into fresh lakes — the union invariant holds every
    round (the lock is not flaky under repeated contention)."""
    pl = pytest.importorskip("polars")
    per_writer = 12
    total = _WRITERS * per_writer
    for round_no in range(min(_ROUNDS, 12)):
        lake_root = tmp_path / f"lake-{round_no}"
        errors: list[BaseException] = []
        barrier = threading.Barrier(_WRITERS)

        def worker(
            w: int,
            _root: Path = lake_root,
            _errs: list[BaseException] = errors,
            _b: threading.Barrier = barrier,
        ) -> None:
            writer = ParquetWriter(lake_root=_root)
            bars = [_bar(i) for i in range(w * per_writer, (w + 1) * per_writer)]
            try:
                _b.wait(timeout=30)
                _write(writer, bars)
            except BaseException as exc:  # surface ANY thread failure
                _errs.append(exc)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(_WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"round {round_no}: a writer raised: {errors[:1]}"
        df = pl.read_parquet(_partition_file(lake_root))
        assert df.height == total, f"round {round_no}: lost update ({df.height}/{total})"
        assert not _stray_tmp_files(lake_root)


# ────── invariant 2: mid-write crash / ENOSPC leaves no partial / corrupt file ──────


def _read_minutes(lake_root: Path, pl: object) -> set[int]:
    df = pl.read_parquet(_partition_file(lake_root))  # type: ignore[attr-defined]
    return {
        int((ts.replace(tzinfo=UTC) - _BASE).total_seconds() // 60)
        for ts in df["timestamp"].to_list()
    }


@pytest.mark.parametrize("seed", [3, 17, 2024])
def test_randomized_midwrite_crash_never_corrupts_partition(
    tmp_path: Path, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a fault at a RANDOM point of the atomic write (fsync vs os.replace)
    on each round and assert the committed partition is never torn.

    Round 0 lands a clean partition (bars 0..K). Each later round attempts a merge
    that adds new bars but faults mid-write; the write must raise — ``LakeWriteHalt``
    on the injected ``ENOSPC`` (the disk-full global stop, DEC-LAKE-WRITE-HALT-001) or
    a raw ``OSError`` on the injected ``EIO`` (unchanged — only ``ENOSPC`` is mapped) —
    and leave the partition EXACTLY as the last successful commit — same bytes, same
    bars — with no ``.tmp`` leak. A clean write after every fault still succeeds and
    advances the committed set, proving the writer is not wedged by the fault.
    """
    pl = pytest.importorskip("polars")
    rng = random.Random(seed)
    lake_root = tmp_path / "lake"
    writer = ParquetWriter(lake_root=lake_root)

    # Round 0: a clean baseline commit.
    committed = list(range(10))
    _write(writer, [_bar(i) for i in committed])
    good_bytes = _partition_file(lake_root).read_bytes()

    real_fsync = os.fsync
    real_replace = os.replace
    next_new = 10
    for _ in range(_ROUNDS):
        new_bars = [_bar(i) for i in range(next_new, next_new + 5)]
        fault_at_replace = rng.random() < 0.5
        errno_code = rng.choice([errno.ENOSPC, errno.EIO])

        def _boom_fsync(fd: int, _e: int = errno_code) -> None:
            raise OSError(_e, os.strerror(_e))

        def _boom_replace(src: str, dst: str, _e: int = errno_code) -> None:
            # The tmp file exists at this point; the writer must clean it up.
            raise OSError(_e, os.strerror(_e))

        if fault_at_replace:
            monkeypatch.setattr(os, "replace", _boom_replace)
        else:
            monkeypatch.setattr(os, "fsync", _boom_fsync)
        try:
            # ENOSPC → LakeWriteHalt (disk-full global stop); EIO → raw OSError
            # (unchanged — only ENOSPC is mapped). Either way the partition is intact.
            with pytest.raises((OSError, LakeWriteHalt)):
                _write(writer, new_bars)
        finally:
            monkeypatch.setattr(os, "fsync", real_fsync)
            monkeypatch.setattr(os, "replace", real_replace)

        # The partition is byte-identical to the last good commit — never torn.
        assert _partition_file(lake_root).read_bytes() == good_bytes
        assert _read_minutes(lake_root, pl) == set(committed)
        assert not _stray_tmp_files(lake_root), "the failed write leaked a .tmp sidecar"

        # A clean write still works (the writer is not wedged) → advance the commit.
        _write(writer, new_bars)
        committed.extend(range(next_new, next_new + 5))
        good_bytes = _partition_file(lake_root).read_bytes()
        assert _read_minutes(lake_root, pl) == set(committed)
        next_new += 5


def test_enospc_on_first_write_leaves_no_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``ENOSPC`` during the very FIRST write to a partition leaves NO file at all
    (and no ``.tmp``) — a fresh partition is all-or-nothing.

    A reader must never discover a brand-new partition that is actually a truncated
    fragment; on a disk-full first write the destination simply does not exist.
    """
    lake_root = tmp_path / "lake"
    writer = ParquetWriter(lake_root=lake_root)

    def _boom_fsync(fd: int) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(os, "fsync", _boom_fsync)
    # ENOSPC at the lake-write boundary is now a graceful LakeWriteHalt global stop
    # (DEC-LAKE-WRITE-HALT-001), not a raw OSError — the partition invariant is the
    # same (all-or-nothing), only the exception type changed.
    with pytest.raises(LakeWriteHalt) as excinfo:
        _write(writer, [_bar(i) for i in range(5)])
    assert excinfo.value.reason == "disk_full"

    monkeypatch.undo()
    assert not list(lake_root.rglob("*.parquet")), "a torn first-write partition was left behind"
    assert not _stray_tmp_files(lake_root), "ENOSPC leaked a .tmp sidecar"

    # The writer recovers once space is back: the same write now lands cleanly.
    _write(writer, [_bar(i) for i in range(5)])
    assert _partition_file(lake_root).exists()


def test_enospc_creating_fresh_partition_dir_halts_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``ENOSPC`` while FIRST-creating a brand-new partition directory raises a
    graceful ``LakeWriteHalt('disk_full')`` — NOT a raw ``OSError``.

    Regression for the spot-writer asymmetry (DEC-LAKE-WRITE-HALT-001 / MIGRATION.md
    #885): the very first write to a partition creates its parent directory via
    ``dest.parent.mkdir(parents=True, exist_ok=True)``. On a full disk that ``mkdir``
    raises ``OSError(ENOSPC)``, and unless it runs inside ``lake_write_guard`` the raw
    ``OSError`` (not a ``BruteXError``) escapes the per-symbol quarantine and the ingest
    spines re-raise it as an "escaped code bug" — crashing the WHOLE pull. The option
    writer already guards its equivalent ``mkdir``; this proves the spot writer now does
    too, so a disk-full at the fresh-partition ``mkdir`` HALTS gracefully like every other
    lake-write failure site. The ``mkdir`` is patched ONLY for the directory-creating
    call (``exist_ok=True`` no-ops once the dir exists), so the round-0 fresh write is
    exactly the failure point.
    """
    lake_root = tmp_path / "lake"
    writer = ParquetWriter(lake_root=lake_root)

    real_mkdir = Path.mkdir

    def _boom_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # The partition's parent dir does not yet exist on the very first write, so this
        # is the genuine fresh-partition mkdir; raise the disk-full the writer must map.
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(Path, "mkdir", _boom_mkdir)
    with pytest.raises(LakeWriteHalt) as excinfo:
        _write(writer, [_bar(i) for i in range(5)])
    assert excinfo.value.reason == "disk_full"

    # Restore mkdir; nothing was created (the mkdir itself failed) and no .tmp leaked.
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert not list(lake_root.rglob("*.parquet")), "a torn first-write partition was left behind"
    assert not _stray_tmp_files(lake_root), "the failed fresh-dir mkdir leaked a .tmp sidecar"

    # The writer recovers once space is back: the same fresh write now lands cleanly.
    _write(writer, [_bar(i) for i in range(5)])
    assert _partition_file(lake_root).exists()

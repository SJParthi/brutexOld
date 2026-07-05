"""Unit tests for the cross-process lake partition write-lock.

DEC-LAKE-PARTITION-FLOCK-001 — :mod:`brutex.data.partition_lock` adds an advisory
``fcntl.flock`` held across a partition's read-modify-write so two PROCESSES (a CLI
``brutex fetch`` + a dashboard pull sharing one lake) can't lost-update the same
month partition. The lock is on a stable ``<lake_root>/.locks/<sha256(dest)>.lock``
sidecar (never the partition file, whose inode ``os.replace`` swaps), per partition,
and a full disk at lock creation maps to a :class:`LakeWriteHalt`. The bounded-acquire
(DEC-PULL-CONTENTION-ANTISTALL-001) tests white-box the module-private ``_acquire_bounded``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
import fcntl
import os
import threading
from pathlib import Path

import pytest
from brutex.data.errors import DataLakeError, LakeWriteHalt, PartitionLockTimeout
from brutex.data.partition_lock import (
    DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS,
    PARTITION_LOCK_TIMEOUT_ENV,
    BoundedLockRegistry,
    _acquire_bounded,
    partition_lock_path,
    partition_write_lock,
    resolve_partition_lock_timeout_seconds,
)


def _raise(exc: BaseException) -> None:
    """Raise *exc* via a CALL, not a literal ``raise`` statement.

    Keeps the assertion AFTER a ``with pytest.raises(...), partition_write_lock(...)``
    block reachable to the static analyzer: a literal ``raise`` as the last with-body
    statement makes CodeQL mark the following assert "unreachable" (a false positive —
    ``pytest.raises`` suppresses the exception). A function call is not treated as
    unconditionally terminating, so the assert stays reachable (mirrors
    ``tests/unit/test_atomic_io.py``).
    """
    raise exc


def _try_lock_held_by_other_fd(lock_path: Path) -> bool:
    """Return True iff a SEPARATE fd cannot take the exclusive lock (i.e. it is held).

    A second ``open`` of the same path is a distinct open file description, so
    ``fcntl.flock`` treats it like another PROCESS — exactly the cross-process
    contention the production lock must win. A non-blocking ``LOCK_EX`` that raises
    ``BlockingIOError`` proves the lock is currently held.
    """
    probe_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(probe_fd)


# ── partition_lock_path ──────────────────────────────────────────────────────
def test_lock_path_is_under_locks_dir(tmp_path: Path) -> None:
    dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)
    assert lock_path.parent == tmp_path / ".locks"
    assert lock_path.suffix == ".lock"
    # The .locks dir is OUTSIDE the bars/ tree and is not a *.parquet file, so no
    # dataset glob / lake scanner ever sees it.
    assert ".parquet" not in lock_path.name


def test_lock_path_is_deterministic_per_partition(tmp_path: Path) -> None:
    """Same dest → same lock file; different dest → different lock file (per-partition)."""
    dest_a = tmp_path / "bars" / "a" / "05.parquet"
    dest_b = tmp_path / "bars" / "b" / "05.parquet"
    assert partition_lock_path(tmp_path, dest_a) == partition_lock_path(tmp_path, dest_a)
    assert partition_lock_path(tmp_path, dest_a) != partition_lock_path(tmp_path, dest_b)


# ── partition_write_lock ─────────────────────────────────────────────────────
def test_lock_creates_locks_dir_and_holds_then_releases(tmp_path: Path) -> None:
    dest = tmp_path / "bars" / "x" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)
    assert not lock_path.exists()

    with partition_write_lock(tmp_path, dest):
        assert lock_path.exists()  # created lazily on first use
        # While held, a separate fd (≈ another process) CANNOT acquire it.
        assert _try_lock_held_by_other_fd(lock_path) is True

    # After the block the lock is released (fd closed) — a fresh acquire succeeds.
    assert _try_lock_held_by_other_fd(lock_path) is False


def test_lock_released_even_when_body_raises(tmp_path: Path) -> None:
    """The lock is released on the error path too (the caller's error still propagates)."""
    dest = tmp_path / "bars" / "y" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)

    with pytest.raises(ValueError, match="boom"), partition_write_lock(tmp_path, dest):
        _raise(ValueError("boom"))

    assert _try_lock_held_by_other_fd(lock_path) is False


def test_different_partitions_do_not_contend(tmp_path: Path) -> None:
    """Holding one partition's lock leaves a DIFFERENT partition free (parallelism kept)."""
    dest_a = tmp_path / "bars" / "a" / "05.parquet"
    dest_b = tmp_path / "bars" / "b" / "05.parquet"
    with partition_write_lock(tmp_path, dest_a):
        # A nested acquire of a DIFFERENT partition does not block.
        with partition_write_lock(tmp_path, dest_b):
            assert _try_lock_held_by_other_fd(partition_lock_path(tmp_path, dest_b)) is True
        assert _try_lock_held_by_other_fd(partition_lock_path(tmp_path, dest_a)) is True


def test_disk_full_at_lock_creation_maps_to_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC opening the lock file maps to LakeWriteHalt (DEC-LAKE-WRITE-HALT-001)."""
    dest = tmp_path / "bars" / "z" / "05.parquet"

    def _full_disk_open(*_a: object, **_k: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.data.partition_lock.os.open", _full_disk_open)
    # The guard maps ENOSPC inside `__enter__` (before the body), so enter directly —
    # a `with ...: <body>` would make the body unreachable dead code.
    cm = partition_write_lock(tmp_path, dest)
    with pytest.raises(LakeWriteHalt) as exc_info:
        cm.__enter__()
    assert exc_info.value.reason == "disk_full"


# ── PartitionLockTimeout (the transient defer signal) ────────────────────────
def test_partition_lock_timeout_is_transient_datalake_error_not_halt() -> None:
    exc = PartitionLockTimeout(12.5)
    assert exc.timeout_seconds == 12.5
    # A DataLakeError (so the spines' BruteXError handling sees it) but NOT a
    # LakeWriteHalt (a window defer, never a pull-wide stop).
    assert isinstance(exc, DataLakeError)
    assert not isinstance(exc, LakeWriteHalt)
    assert "12.5" in str(exc)
    assert "deferred" in str(exc)


# ── resolve_partition_lock_timeout_seconds (env-tunable deadline) ─────────────
def test_timeout_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PARTITION_LOCK_TIMEOUT_ENV, raising=False)
    assert resolve_partition_lock_timeout_seconds() == DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
    # The audited default is well under the 600 s pull watchdog (the anti-stall point).
    assert DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS < 600.0


@pytest.mark.parametrize("raw", ["", "   ", "not-a-number", "0", "-5", "nan", "NAN", "inf", "-inf"])
def test_timeout_falls_back_on_bad_value(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank / non-numeric / non-positive / non-finite can never disable the bound.

    ``nan`` / ``inf`` parse as floats but would DEFEAT the bound (a ``nan`` deadline makes
    ``clock() >= deadline`` perpetually False; an ``inf`` deadline never expires) — both
    must fall back to the audited default (DEC-PULL-CONTENTION-ANTISTALL-001 fail-safe).
    """
    monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, raw)
    assert resolve_partition_lock_timeout_seconds() == DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS


def test_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PARTITION_LOCK_TIMEOUT_ENV, "42.5")
    assert resolve_partition_lock_timeout_seconds() == 42.5


# ── _acquire_bounded (the bounded, never-blocking acquire) ────────────────────
def test_acquire_succeeds_first_try(tmp_path: Path) -> None:
    """An uncontended lock is taken on the first non-blocking attempt (no sleep)."""
    fd = os.open(tmp_path / "x.lock", os.O_CREAT | os.O_RDWR, 0o600)
    slept: list[float] = []
    try:
        _acquire_bounded(fd, timeout_seconds=1.0, sleep=slept.append)
    finally:
        os.close(fd)
    assert slept == []  # acquired immediately, never polled


def test_acquire_retries_then_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer holding the lock makes the bounded acquire POLL, then win when released."""
    lock_path = tmp_path / "y.lock"
    held_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a peer holds it

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    beats: list[int] = []
    monkeypatch.setattr("brutex.data.partition_lock.emit_progress_beat", lambda: beats.append(1))

    def _release_after_two(_secs: float) -> None:
        # Drop the peer's lock on the SECOND poll so the next attempt wins.
        if len(beats) >= 2:
            fcntl.flock(held_fd, fcntl.LOCK_UN)

    try:
        _acquire_bounded(fd, timeout_seconds=10.0, poll_seconds=0.0, sleep=_release_after_two)
    finally:
        os.close(fd)
        os.close(held_fd)
    # It polled (beat fired) at least twice before winning — a liveness beat per poll
    # keeps the watchdog alive (DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15).
    assert len(beats) >= 2


def test_acquire_raises_partition_lock_timeout_on_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged peer that never releases → PartitionLockTimeout, never an infinite block."""
    lock_path = tmp_path / "z.lock"
    held_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # wedged peer, never releases
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

    # A fake monotonic clock that jumps PAST the deadline after the first poll, so the
    # test never really sleeps yet the deadline branch is exercised deterministically.
    ticks = iter([0.0, 0.0, 100.0])

    def _clock() -> float:
        return next(ticks)

    beats: list[int] = []
    monkeypatch.setattr("brutex.data.partition_lock.emit_progress_beat", lambda: beats.append(1))
    try:
        with pytest.raises(PartitionLockTimeout) as exc_info:
            _acquire_bounded(
                fd, timeout_seconds=1.0, poll_seconds=0.0, clock=_clock, sleep=lambda _s: None
            )
    finally:
        os.close(fd)
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    assert exc_info.value.timeout_seconds == 1.0
    # A liveness beat fired on each poll before the deadline expired (the clock ticks:
    # one for the deadline baseline, then 0.0 < 1.0 → beat+sleep, then 100.0 ≥ 1.0 → raise).
    assert beats == [1, 1]


def test_partition_write_lock_times_out_against_wedged_peer(tmp_path: Path) -> None:
    """End-to-end: the public CM raises the bounded-timeout transient, not a block."""
    dest = tmp_path / "bars" / "w" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a wedged peer holds it
    try:
        cm = partition_write_lock(tmp_path, dest, timeout_seconds=0.05)
        with pytest.raises(PartitionLockTimeout):
            cm.__enter__()
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)


# ── BoundedLockRegistry (GAP #19) ────────────────────────────────────────────
class TestBoundedLockRegistry:
    """The capacity-bounded per-key lock intern (GAP #19).

    The lake writers used to intern one ``threading.Lock`` per partition path in a plain
    ``dict`` that grew FOREVER — a full multi-year pull could accumulate 10^5–10^6 ``Lock``
    objects. This registry caps the resident set with LRU eviction of IDLE locks, while
    NEVER evicting an in-use (refcounted) lock, so correctness is independent of the cap.
    """

    def test_rejects_non_positive_capacity(self) -> None:
        with pytest.raises(ValueError, match="capacity must be >= 1"):
            BoundedLockRegistry(capacity=0)

    def test_capacity_and_initial_size(self) -> None:
        reg = BoundedLockRegistry(capacity=8)
        assert reg.capacity == 8
        assert reg.size() == 0

    def test_same_key_serialises(self) -> None:
        """The same key gets ONE shared lock — a nested non-blocking acquire FAILS while held."""
        reg = BoundedLockRegistry(capacity=4)
        with reg.acquire("p"):
            with reg._guard:  # pyright: ignore[reportPrivateUsage]
                entry = reg._locks["p"]  # pyright: ignore[reportPrivateUsage]
                assert entry.refs == 1  # the holder is refcounted (so it can't be evicted)
            nested_acquired = entry.lock.acquire(blocking=False)
            assert nested_acquired is False
        # Released + refcount dropped after the block → idle (evictable).
        with reg._guard:  # pyright: ignore[reportPrivateUsage]
            assert reg._locks["p"].refs == 0  # pyright: ignore[reportPrivateUsage]

    def test_idle_locks_are_evicted_when_over_capacity(self) -> None:
        """A pull touching MANY distinct partitions keeps the resident set bounded by the cap."""
        reg = BoundedLockRegistry(capacity=2)
        for i in range(50):  # 50 distinct one-shot partitions, each used then released
            with reg.acquire(f"partition-{i}"):
                pass
        # Idle locks were LRU-evicted down to the cap — not 50 leaked Lock objects.
        assert reg.size() <= 2

    def test_in_use_lock_is_never_evicted(self) -> None:
        """An IN-USE (held) lock survives eviction pressure — correctness over the cap.

        Hold one key's lock, then churn many OTHER keys past the cap. The held lock must
        NOT be dropped + re-created (which would split mutual exclusion across two objects).
        """
        reg = BoundedLockRegistry(capacity=1)
        with reg.acquire("held"):
            with reg._guard:  # pyright: ignore[reportPrivateUsage]
                held_entry = reg._locks["held"]  # pyright: ignore[reportPrivateUsage]
            # Churn many other one-shot keys, each blowing past capacity=1.
            for i in range(20):
                with reg.acquire(f"other-{i}"):
                    pass
            # The held key is STILL the SAME interned object (never evicted while in use).
            with reg._guard:  # pyright: ignore[reportPrivateUsage]
                assert reg._locks["held"] is held_entry  # pyright: ignore[reportPrivateUsage]
                assert reg._locks["held"].refs == 1  # pyright: ignore[reportPrivateUsage]

    def test_concurrent_same_key_threads_mutually_exclude(self) -> None:
        """Two threads acquiring the SAME key never overlap inside the critical section."""
        reg = BoundedLockRegistry(capacity=4)
        order: list[str] = []
        start = threading.Barrier(2)
        inside = threading.Lock()
        overlap = {"seen": False}

        def _worker(name: str) -> None:
            start.wait()
            with reg.acquire("shared"):
                # If another thread is already inside, the section overlapped (a bug).
                if not inside.acquire(blocking=False):
                    overlap["seen"] = True
                else:
                    order.append(name)
                    inside.release()

        t1 = threading.Thread(target=_worker, args=("a",))
        t2 = threading.Thread(target=_worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert overlap["seen"] is False
        assert sorted(order) == ["a", "b"]


# ── partition_lock_path realpath canonicalisation (DEC-PARTITION-LOCK-REALPATH-001) ──


class TestLockPathRealpathCanonicalisation:
    """Path aliases to ONE physical partition key the SAME lock file; the
    already-canonical digest is byte-unchanged (no cross-version split-lock)."""

    def test_symlinked_lake_root_alias_maps_to_same_lock(self, tmp_path: Path) -> None:
        # FAILS BEFORE THE FIX: a symlinked lake root spelled a different raw
        # path string → a different lock file → NO mutual exclusion.
        real_root = tmp_path / "lake"
        (real_root / "bars" / "x").mkdir(parents=True)
        alias_root = tmp_path / "lake-alias"
        alias_root.symlink_to(real_root, target_is_directory=True)
        dest_real = real_root / "bars" / "x" / "05.parquet"
        dest_alias = alias_root / "bars" / "x" / "05.parquet"
        assert dest_real != dest_alias  # genuinely different spellings…
        assert (
            partition_lock_path(real_root, dest_real).name
            == partition_lock_path(real_root, dest_alias).name
        )  # …one lock file.

    def test_dotdot_component_alias_maps_to_same_lock(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "bars" / "x").mkdir(parents=True)
        dest = base / "bars" / "x" / "05.parquet"
        dest_dotdot = base / "bars" / ".." / "bars" / "x" / "05.parquet"
        assert partition_lock_path(base, dest).name == partition_lock_path(base, dest_dotdot).name

    def test_relative_dest_maps_to_same_lock_as_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = tmp_path.resolve()
        (base / "bars" / "x").mkdir(parents=True)
        monkeypatch.chdir(base)
        dest_abs = base / "bars" / "x" / "05.parquet"
        dest_rel = Path("bars") / "x" / "05.parquet"
        assert partition_lock_path(base, dest_abs).name == partition_lock_path(base, dest_rel).name

    def test_canonical_absolute_path_digest_byte_unchanged(self, tmp_path: Path) -> None:
        # PIN: for an already-canonical absolute dest (no symlink / .. / relative
        # prefix) the digest equals sha256(str(dest)) — the PRE-fix scheme — so
        # existing on-disk lock-file names are preserved across the upgrade.
        import hashlib

        base = tmp_path.resolve()
        (base / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025").mkdir(parents=True)
        dest = base / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "05.parquet"
        expected = hashlib.sha256(str(dest).encode("utf-8")).hexdigest()
        assert partition_lock_path(base, dest).name == f"{expected}.lock"

    def test_nonexistent_dest_tail_still_keys_consistently(self, tmp_path: Path) -> None:
        # The partition file does not exist yet on FIRST write — realpath keeps
        # the non-existent tail, so pre-create and post-create keys agree.
        base = tmp_path.resolve()
        dest = base / "bars" / "new" / "05.parquet"  # nothing created.
        before = partition_lock_path(base, dest)
        (base / "bars" / "new").mkdir(parents=True)
        dest.write_bytes(b"")
        assert partition_lock_path(base, dest) == before

    def test_different_partitions_still_differ(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        assert partition_lock_path(base, base / "bars" / "a" / "05.parquet") != partition_lock_path(
            base, base / "bars" / "b" / "05.parquet"
        )

    def test_cross_alias_mutual_exclusion_end_to_end(self, tmp_path: Path) -> None:
        # The actual guarantee: a writer entering via the SYMLINK alias is
        # excluded by a holder that entered via the real path.
        real_root = tmp_path / "lake"
        (real_root / "bars" / "x").mkdir(parents=True)
        alias_root = tmp_path / "alias"
        alias_root.symlink_to(real_root, target_is_directory=True)
        dest_real = real_root / "bars" / "x" / "05.parquet"
        dest_alias = alias_root / "bars" / "x" / "05.parquet"
        with partition_write_lock(real_root, dest_real):
            probe = partition_lock_path(real_root, dest_alias)
            assert _try_lock_held_by_other_fd(probe) is True  # alias spelling blocked.

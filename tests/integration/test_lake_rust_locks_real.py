"""REAL-crate behavioral proofs for the PR-4 lock/atomic primitives.

DEC-LAKE-RUST-LOCKS-ATOMIC-001 (MIGRATION.md #1912). These tests drive the
COMPILED ``brutex_lake`` wheel — the load-bearing gates the fake-crate unit
suite (`tests/unit/test_lake_rust_locks.py`) structurally cannot prove:

* the MIXED-ARM mutual-exclusion law — an flock is held per OPEN FILE
  DESCRIPTION (the #1785 G10 lesson), so a Rust-arm holder excludes a
  Python-arm waiter (and vice versa) even inside ONE process, plus the
  two-PROCESS belt in both directions;
* the SAME-PATH law — both arms lock the byte-identical
  ``partition_lock_path`` sidecar (the crate receives ONLY the fd, so a
  path re-derivation in Rust is structurally impossible; pinned here
  end-to-end anyway);
* errno INTACTNESS through the FFI (``EBADF``/``ENOENT``/real-FS rename
  fault parity — the ``lake_write_guard`` taxonomy's precondition);
* the routed ``partition_write_lock`` + ``atomic_write_via`` end-to-end
  with the flag ON over the real crate.

Wheel-free runners SKIP this module cleanly (``importorskip``); a PRE-PR-4
(stale) wheel without the lock symbols also skips — the facade's own
API-surface guard covers that shape at runtime (unit-pinned).
"""

# pyright: reportPrivateUsage=none
# (whitebox suite — the documented file-top pattern, pyproject.toml DEC-P0-017)

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import brutex.data.lake_rust_locks as lrl
import pytest
from brutex.data import atomic_io, partition_lock
from brutex.data.atomic_io import atomic_write_via
from brutex.data.errors import PartitionLockTimeout
from brutex.data.partition_lock import partition_lock_path, partition_write_lock
from structlog.testing import capture_logs

brutex_lake = pytest.importorskip("brutex_lake")
if not hasattr(brutex_lake, "lock_try_exclusive"):  # pragma: no cover - env-dependent
    pytest.skip(
        "installed brutex_lake wheel predates ADR-0020 PR-4 (no lock symbols)",
        allow_module_level=True,
    )

#: Bounded wait for every cross-process synchronisation below — the suite
#: must never hang a runner (fail loudly instead).
_DEADLINE_S = 20.0


@pytest.fixture(autouse=True)
def _reset_leaf() -> object:
    lrl.reset_lake_rust_locks_for_tests()
    yield None
    lrl.reset_lake_rust_locks_for_tests()


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)


class TestMixedArmMutualExclusion:
    """The load-bearing gate: Rust arm ⟷ Python arm exclude each other."""

    def test_rust_holder_excludes_python_waiter_then_timeout(self, tmp_path: Path) -> None:
        """(a) Rust holds → the Python `_acquire_bounded` waiter times out
        with TODAY's `PartitionLockTimeout` message bytes."""
        lock = tmp_path / "lock.sidecar"
        holder_fd = _open_lock(lock)
        waiter_fd = -1
        try:
            waiter_fd = _open_lock(lock)  # a SECOND open file description
            assert brutex_lake.lock_try_exclusive(holder_fd) is True
            clock_values = iter([0.0, 0.05, 0.11])
            with pytest.raises(PartitionLockTimeout) as exc_info:
                partition_lock._acquire_bounded(
                    waiter_fd,
                    timeout_seconds=0.1,
                    clock=lambda: next(clock_values),
                    sleep=lambda _s: None,
                )
            assert str(exc_info.value) == str(PartitionLockTimeout(0.1))
            brutex_lake.lock_release(holder_fd)  # CONSUMES the fd (LOCK_UN + close)
            holder_fd = -1
        finally:
            # Ownership-tracked cleanup: `lock_release` consumed holder_fd on
            # the happy path; a bare close here would double-close (and with
            # fd-number reuse could hit an unrelated fd).
            if holder_fd >= 0:
                os.close(holder_fd)
            if waiter_fd >= 0:
                os.close(waiter_fd)

    def test_python_holder_excludes_rust_waiter(self, tmp_path: Path) -> None:
        """(b) roles swapped: Python holds via fcntl → the Rust attempt reads False."""
        lock = tmp_path / "lock.sidecar"
        holder_fd = _open_lock(lock)
        waiter_fd = -1
        try:
            waiter_fd = _open_lock(lock)
            fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert brutex_lake.lock_try_exclusive(waiter_fd) is False
        finally:
            os.close(holder_fd)
            if waiter_fd >= 0:
                os.close(waiter_fd)

    def test_release_hands_the_lock_to_the_other_arm(self, tmp_path: Path) -> None:
        """(c) Rust release → the Python waiter acquires; and the inverse."""
        lock = tmp_path / "lock.sidecar"
        rust_fd = _open_lock(lock)
        py_fd = -1
        rust_fd2 = -1
        try:
            py_fd = _open_lock(lock)
            assert brutex_lake.lock_try_exclusive(rust_fd) is True
            assert partition_lock._flock_attempt(py_fd) is False
            brutex_lake.lock_release(rust_fd)  # CONSUMES rust_fd (LOCK_UN + close)
            rust_fd = -1
            assert partition_lock._flock_attempt(py_fd) is True
            # Inverse: python holds, rust waiter blocked, python close releases.
            rust_fd2 = _open_lock(lock)
            assert brutex_lake.lock_try_exclusive(rust_fd2) is False
            os.close(py_fd)
            py_fd = -1
            assert brutex_lake.lock_try_exclusive(rust_fd2) is True
            brutex_lake.lock_release(rust_fd2)  # CONSUMES rust_fd2
            rust_fd2 = -1
        finally:
            # Only close what was NOT consumed/closed above — a blind close
            # would double-close a released fd (fd-number reuse hazard).
            for leftover in (rust_fd, py_fd, rust_fd2):
                if leftover >= 0:
                    os.close(leftover)

    def test_two_process_belt_python_holder_rust_waiter(self, tmp_path: Path) -> None:
        """The cross-PROCESS belt: a subprocess Python holder excludes this
        process's Rust waiter; its release hands the lock over."""
        lock = tmp_path / "xproc.sidecar"
        child_code = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('HELD', flush=True)\n"
            "sys.stdin.readline()\n"
            "os.close(fd)\n"
            "print('RELEASED', flush=True)\n"
            "sys.stdin.readline()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stdout is not None and proc.stdin is not None
            assert proc.stdout.readline().strip() == "HELD"
            fd = _open_lock(lock)
            try:
                assert brutex_lake.lock_try_exclusive(fd) is False  # held cross-process
                proc.stdin.write("\n")
                proc.stdin.flush()
                assert proc.stdout.readline().strip() == "RELEASED"
                deadline = time.monotonic() + _DEADLINE_S
                acquired = False
                while time.monotonic() < deadline:
                    if brutex_lake.lock_try_exclusive(fd):
                        acquired = True
                        break
                    time.sleep(0.01)
                assert acquired, "rust waiter never acquired after the peer's release"
                brutex_lake.lock_release(fd)  # CONSUMES the fd (LOCK_UN + close)
                fd = -1
            finally:
                # A failure path never reached `lock_release`; the plain close
                # releases the flock kernel-side. Never both (double-close).
                if fd >= 0:
                    os.close(fd)
            proc.stdin.write("\n")
            proc.stdin.flush()
            assert proc.wait(timeout=_DEADLINE_S) == 0

    def test_two_process_belt_rust_holder_python_waiter(self, tmp_path: Path) -> None:
        """The inverse belt: a subprocess RUST holder excludes this process's
        Python attempt (the wheel resolves in the child via the inherited env)."""
        lock = tmp_path / "xproc2.sidecar"
        child_code = (
            "import os, sys\n"
            "import brutex_lake\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "assert brutex_lake.lock_try_exclusive(fd) is True\n"
            "print('HELD', flush=True)\n"
            "sys.stdin.readline()\n"
            "brutex_lake.lock_release(fd)\n"
            "print('RELEASED', flush=True)\n"
            "sys.stdin.readline()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stdout is not None and proc.stdin is not None
            assert proc.stdout.readline().strip() == "HELD"
            fd = _open_lock(lock)
            try:
                assert partition_lock._flock_attempt(fd) is False
                proc.stdin.write("\n")
                proc.stdin.flush()
                assert proc.stdout.readline().strip() == "RELEASED"
                deadline = time.monotonic() + _DEADLINE_S
                acquired = False
                while time.monotonic() < deadline:
                    if partition_lock._flock_attempt(fd):
                        acquired = True
                        break
                    time.sleep(0.01)
                assert acquired, "python waiter never acquired after the rust release"
            finally:
                os.close(fd)
            proc.stdin.write("\n")
            proc.stdin.flush()
            assert proc.wait(timeout=_DEADLINE_S) == 0


class TestErrnoIntactThroughFfi:
    """The taxonomy precondition: real errnos survive the pyo3 crossing."""

    def test_dead_fd_is_oserror_ebadf(self, tmp_path: Path) -> None:
        fd = _open_lock(tmp_path / "dead.lock")
        os.close(fd)
        with pytest.raises(OSError) as exc_info:
            brutex_lake.lock_try_exclusive(fd)
        assert exc_info.value.errno == errno.EBADF
        with pytest.raises(OSError) as exc_info:
            brutex_lake.lock_release(fd)
        assert exc_info.value.errno == errno.EBADF

    def test_missing_tmp_is_filenotfound_parity(self, tmp_path: Path) -> None:
        """Direct-primitive parity: crate commit vs the Python oracle raise the
        SAME exception class + errno for a missing temp."""
        dest = tmp_path / "d.parquet"
        missing = tmp_path / "no-such.tmp"
        with pytest.raises(FileNotFoundError) as rust_exc:
            brutex_lake.atomic_commit_replace(str(missing), str(dest), None)
        with pytest.raises(FileNotFoundError) as py_exc:
            atomic_io._python_commit_replace(missing, dest, None)
        assert rust_exc.value.errno == py_exc.value.errno == errno.ENOENT
        assert not dest.exists()

    def test_rename_onto_directory_fault_parity(self, tmp_path: Path) -> None:
        """A real-FS rename fault (dest is a non-empty DIRECTORY) surfaces the
        SAME errno from both arms — and both leave the temp for cleanup."""
        dest = tmp_path / "dest-is-a-dir"
        (dest / "occupant").mkdir(parents=True)
        tmp_a = tmp_path / ".brutex-write-a.parquet.tmp"
        tmp_b = tmp_path / ".brutex-write-b.parquet.tmp"
        tmp_a.write_bytes(b"x")
        tmp_b.write_bytes(b"x")
        with pytest.raises(OSError) as rust_exc:
            brutex_lake.atomic_commit_replace(str(tmp_a), str(dest), None)
        with pytest.raises(OSError) as py_exc:
            atomic_io._python_commit_replace(tmp_b, dest, None)
        assert rust_exc.value.errno == py_exc.value.errno
        assert tmp_a.exists() and tmp_b.exists()  # the primitive never unlinks


class TestRoutedEndToEndOnArm:
    """Flag ON over the REAL crate — the production seams end-to-end."""

    def test_partition_write_lock_holds_and_releases_via_rust(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(lrl.LAKE_RUST_LOCKS_ENV, "1")
        dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01.parquet"
        sidecar = partition_lock_path(tmp_path, dest)
        with capture_logs() as logs, partition_write_lock(tmp_path, dest):
            assert lrl.rust_locks_disabled() is False  # genuinely engaged
            probe = os.open(sidecar, os.O_RDWR)
            try:
                # A second OFD is refused WHILE held (the mixed-arm probe).
                assert partition_lock._flock_attempt(probe) is False
            finally:
                os.close(probe)
        assert any(e["event"] == "lake_rust_locks.engaged" for e in logs)
        # Released on exit: a fresh OFD acquires immediately.
        probe = os.open(sidecar, os.O_RDWR)
        try:
            assert partition_lock._flock_attempt(probe) is True
        finally:
            os.close(probe)

    def test_atomic_write_via_lands_bytes_and_cleans_tmp(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(lrl.LAKE_RUST_LOCKS_ENV, "1")
        dest = tmp_path / "2024-01.parquet"
        dest.write_bytes(b"stale")
        atomic_write_via(dest, lambda t: t.write_bytes(b"routed payload"))
        assert lrl.rust_locks_disabled() is False
        assert dest.read_bytes() == b"routed payload"
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_atomic_write_chmod_arm_via_rust(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import stat as _stat

        monkeypatch.setenv(lrl.LAKE_RUST_LOCKS_ENV, "1")
        dest = tmp_path / "credentials.json"
        atomic_io.atomic_write_simple(dest, lambda t: t.write_text("s"), chmod=0o600)
        assert _stat.S_IMODE(dest.stat().st_mode) == 0o600
        assert lrl.rust_locks_disabled() is False

    def test_on_off_arm_bytes_identical(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The behavior-parity pin standing in for the (deliberately dropped)
        byte burn-in: the same payload lands byte-identically on both arms."""
        payload = os.urandom(4096)
        off_dest = tmp_path / "off.parquet"
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        atomic_write_via(off_dest, lambda t: t.write_bytes(payload))
        on_dest = tmp_path / "on.parquet"
        monkeypatch.setenv(lrl.LAKE_RUST_LOCKS_ENV, "1")
        atomic_write_via(on_dest, lambda t: t.write_bytes(payload))
        assert lrl.rust_locks_disabled() is False
        assert off_dest.read_bytes() == on_dest.read_bytes() == payload

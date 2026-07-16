"""Unit gate for :mod:`brutex.data.lake_rust_locks` (ADR-0020 PR-4).

DEC-LAKE-RUST-LOCKS-ATOMIC-001 (MIGRATION.md #1912). FAKE-crate driven — a
stand-in ``brutex_lake`` module installed in ``sys.modules`` (or a poisoned
``None`` entry for the import-guard arm) exercises every facade path with
NO compiled wheel, so this file alone proves the leaf 100% line+branch on
the wheel-free CI runners. The REAL-crate behavioral proofs (mixed-arm
mutual exclusion, the two-subprocess belt, real-FS errno parity) live in
``tests/integration/test_lake_rust_locks_real.py`` behind an
``importorskip`` — this file owns the flag matrix, the degrade taxonomy,
the routing seams and the OFF-arm identity pins.
"""

# pyright: reportPrivateUsage=none
# (whitebox suite — the documented file-top pattern, pyproject.toml DEC-P0-017)

from __future__ import annotations

import errno
import os
import stat
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import brutex.data.lake_rust_locks as lrl
import pytest
from brutex.data import atomic_io, partition_lock
from brutex.data.atomic_io import atomic_write_simple, atomic_write_via
from brutex.data.errors import LakeWriteHalt, PartitionLockTimeout
from brutex.data.partition_lock import partition_lock_path, partition_write_lock
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(autouse=True)
def _reset_leaf() -> Iterator[None]:
    """Latch + warn-memo isolation around EVERY test (the render-suite idiom)."""
    lrl.reset_lake_rust_locks_for_tests()
    yield
    lrl.reset_lake_rust_locks_for_tests()


def _fake_crate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lock_try_exclusive: Callable[..., object] | None = None,
    lock_release: Callable[..., object] | None = None,
    atomic_commit_replace: Callable[..., object] | None = None,
    drop: tuple[str, ...] = (),
) -> types.ModuleType:
    """Install a fake ``brutex_lake`` in ``sys.modules`` and return it.

    Unspecified API members default to loud bombs (an unexpected crate call
    must FAIL the test, never silently succeed); *drop* removes names so the
    import guard's API-surface (stale-wheel) arm is drivable.
    """

    def _bomb(*_a: object, **_k: object) -> object:
        raise AssertionError("fake crate function called unexpectedly")

    module = types.ModuleType("brutex_lake")
    module.lock_try_exclusive = lock_try_exclusive or _bomb  # type: ignore[attr-defined]
    module.lock_release = lock_release or _bomb  # type: ignore[attr-defined]
    module.atomic_commit_replace = atomic_commit_replace or _bomb  # type: ignore[attr-defined]
    for name in drop:
        delattr(module, name)
    monkeypatch.setitem(sys.modules, "brutex_lake", module)
    return module


def _enable(monkeypatch: pytest.MonkeyPatch, value: str = "1") -> None:
    monkeypatch.setenv(lrl.LAKE_RUST_LOCKS_ENV, value)


class _FakePanic(BaseException):
    """A ``PanicException``-shaped ``BaseException`` for the belt arm."""


def _install_panic_home(monkeypatch: pytest.MonkeyPatch) -> type[BaseException]:
    """Register a fake ``pyo3_runtime`` so ``_panic_belt_errors`` resolves it."""
    home = types.ModuleType("pyo3_runtime")
    home.PanicException = _FakePanic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyo3_runtime", home)
    return _FakePanic


class TestFlagMatrix:
    """``BRUTEX_LAKE_RUST_LOCKS`` — default OFF, truthy ON, junk LOUD-OFF."""

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        assert lrl.lake_rust_locks_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        _enable(monkeypatch, raw)
        assert lrl.lake_rust_locks_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
    def test_falsy_is_silently_off(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        _enable(monkeypatch, raw)
        with capture_logs() as logs:
            assert lrl.lake_rust_locks_enabled() is False
        assert logs == []

    def test_blank_is_silently_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch, "   ")
        with capture_logs() as logs:
            assert lrl.lake_rust_locks_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch, "ture")
        with capture_logs() as logs:
            assert lrl.lake_rust_locks_enabled() is False
            assert lrl.lake_rust_locks_enabled() is False  # memoised — ONE warn
        warns = [e for e in logs if e["event"] == "lake_rust_locks.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"


class TestResolveCrate:
    """Import guard: absent wheel / stale API surface latch loud-OFF."""

    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", None)  # import → ImportError
        with capture_logs() as logs:
            assert lrl._resolve_crate() is None
        assert lrl.rust_locks_disabled() is True
        events = [e["event"] for e in logs]
        assert "lake_rust_locks.rust_path_disabled" in events

    def test_stale_wheel_missing_lock_symbols_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PRE-PR-4 wheel (render-only, no lock symbols) is refused loudly."""
        _fake_crate(monkeypatch, drop=("lock_try_exclusive",))
        with capture_logs() as logs:
            assert lrl._resolve_crate() is None
        assert lrl.rust_locks_disabled() is True
        assert any(e["event"] == "lake_rust_locks.rust_path_disabled" for e in logs)

    def test_healthy_fake_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _fake_crate(monkeypatch)
        assert lrl._resolve_crate() is module
        assert lrl.rust_locks_disabled() is False

    def test_disable_latch_is_set_once(self) -> None:
        lrl._disable("first")
        with capture_logs() as logs:
            lrl._disable("second", error=True)  # no-op — already latched
        assert logs == []
        assert lrl.rust_locks_disabled() is True

    def test_reset_seam_clears_latch_and_memo(self) -> None:
        lrl._disable("poison")
        lrl._warn_once("some_reason")
        lrl.reset_lake_rust_locks_for_tests()
        assert lrl.rust_locks_disabled() is False
        with capture_logs() as logs:
            lrl._warn_once("some_reason")  # memo cleared → warns again
        assert len(logs) == 1


class TestPanicBeltResolution:
    """``_panic_belt_errors`` — guarded name resolution, Exception always in."""

    def test_exception_is_always_a_member(self) -> None:
        assert Exception in lrl._panic_belt_errors()

    def test_fake_pyo3_runtime_panic_class_is_folded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        panic = _install_panic_home(monkeypatch)
        assert panic in lrl._panic_belt_errors()

    def test_non_class_panic_attr_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = types.ModuleType("pyo3_runtime")
        home.PanicException = "not a class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", home)
        assert lrl._panic_belt_errors() == (Exception,) or _FakePanic not in (
            lrl._panic_belt_errors()
        )


class TestRustFlockAttempt:
    """The routed one-shot attempt: identity OFF, per-call degrade ON."""

    def test_off_returns_python_attempt_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)

        def oracle(_fd: int) -> bool:
            return True

        assert lrl.rust_flock_attempt(oracle) is oracle

    def test_latched_returns_python_attempt_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        lrl._disable("prior fault")

        def oracle(_fd: int) -> bool:
            return True

        assert lrl.rust_flock_attempt(oracle) is oracle

    def test_unresolvable_crate_returns_python_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setitem(sys.modules, "brutex_lake", None)

        def oracle(_fd: int) -> bool:
            return True

        assert lrl.rust_flock_attempt(oracle) is oracle
        assert lrl.rust_locks_disabled() is True

    def test_engaged_attempt_returns_crate_verdicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        verdicts = iter([True, False])
        calls: list[int] = []

        def fake_try(fd: int) -> bool:
            calls.append(fd)
            return next(verdicts)

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: pytest.fail("oracle must not run"))
        with capture_logs() as logs:
            assert attempt(7) is True
            assert attempt(7) is False
        assert calls == [7, 7]  # fd-only FFI: the crate never sees a path
        assert any(e["event"] == "lake_rust_locks.engaged" for e in logs)

    def test_blocking_io_error_folds_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise BlockingIOError(errno.EAGAIN, "would block")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: pytest.fail("oracle must not run"))
        assert attempt(3) is False
        assert lrl.rust_locks_disabled() is False  # a would-block is never a fault

    def test_real_oserror_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise OSError(errno.EBADF, "bad fd")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: pytest.fail("oracle must not run"))
        with pytest.raises(OSError) as exc_info:
            attempt(3)
        assert exc_info.value.errno == errno.EBADF
        assert lrl.rust_locks_disabled() is False  # errno taxonomy — never latched

    def test_memory_error_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise MemoryError

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: pytest.fail("oracle must not run"))
        with pytest.raises(MemoryError):
            attempt(3)
        assert lrl.rust_locks_disabled() is False

    def test_crate_fault_degrades_to_oracle_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise RuntimeError("brutex-lake lock_try_exclusive panicked")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        oracle_calls: list[int] = []

        def oracle(fd: int) -> bool:
            oracle_calls.append(fd)
            return True

        attempt = lrl.rust_flock_attempt(oracle)
        with capture_logs() as logs:
            assert attempt(5) is True  # the oracle's verdict, same call
        assert oracle_calls == [5]
        assert lrl.rust_locks_disabled() is True
        assert any(e["event"] == "lake_rust_locks.locks_autoreverted" for e in logs)
        # Mid-loop convergence: the SAME wrapper now short-circuits to the oracle.
        assert attempt(6) is True
        assert oracle_calls == [5, 6]

    def test_panic_belt_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        panic = _install_panic_home(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise panic("rust panic escaped catch_unwind")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: False)
        with capture_logs() as logs:
            assert attempt(4) is False
        assert lrl.rust_locks_disabled() is True
        assert any(
            e["event"] == "lake_rust_locks.locks_autoreverted" and e["reason"] == "crate_panic"
            for e in logs
        )

    def test_unknown_exception_takes_the_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        class Weird(Exception):
            pass

        def fake_try(_fd: int) -> bool:
            raise Weird("out-of-taxonomy crate fault")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        attempt = lrl.rust_flock_attempt(lambda _fd: True)
        assert attempt(9) is True
        assert lrl.rust_locks_disabled() is True


class TestRustFlockRelease:
    """The routed release: python_close OFF, crate ON, best-effort fallback."""

    def test_off_calls_python_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        closed: list[int] = []
        lrl.rust_flock_release(11, python_close=closed.append)
        assert closed == [11]

    def test_latched_calls_python_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        lrl._disable("prior fault")
        closed: list[int] = []
        lrl.rust_flock_release(12, python_close=closed.append)
        assert closed == [12]

    def test_unresolvable_crate_calls_python_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        closed: list[int] = []
        lrl.rust_flock_release(13, python_close=closed.append)
        assert closed == [13]

    def test_engaged_release_uses_crate_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        released: list[int] = []
        _fake_crate(monkeypatch, lock_release=released.append)
        lrl.rust_flock_release(14, python_close=lambda _fd: pytest.fail("must not close twice"))
        assert released == [14]

    def test_close_oserror_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def fake_release(_fd: int) -> None:
            raise OSError(errno.EBADF, "close failed")

        _fake_crate(monkeypatch, lock_release=fake_release)
        with pytest.raises(OSError) as exc_info:
            lrl.rust_flock_release(15, python_close=lambda _fd: None)
        assert exc_info.value.errno == errno.EBADF
        assert lrl.rust_locks_disabled() is False

    def test_crate_fault_best_effort_close_suppresses_ebadf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)

        def fake_release(_fd: int) -> None:
            raise RuntimeError("panic-mapped fault")

        _fake_crate(monkeypatch, lock_release=fake_release)

        def python_close(_fd: int) -> None:
            raise OSError(errno.EBADF, "already closed by the faulted crate")

        lrl.rust_flock_release(16, python_close=python_close)  # must NOT raise
        assert lrl.rust_locks_disabled() is True

    def test_panic_belt_falls_back_to_python_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        panic = _install_panic_home(monkeypatch)

        def fake_release(_fd: int) -> None:
            raise panic("boom")

        _fake_crate(monkeypatch, lock_release=fake_release)
        closed: list[int] = []
        lrl.rust_flock_release(17, python_close=closed.append)
        assert closed == [17]
        assert lrl.rust_locks_disabled() is True


class TestRustAtomicCommit:
    """The routed commit: oracle OFF, crate ON, fault → oracle over the SAME tmp."""

    def test_off_runs_python_commit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        calls: list[tuple[Path, Path, int | None]] = []
        lrl.rust_atomic_commit(
            tmp_path / "t",
            tmp_path / "d",
            0o600,
            python_commit=lambda t, d, c: calls.append((t, d, c)),
        )
        assert calls == [(tmp_path / "t", tmp_path / "d", 0o600)]

    def test_latched_runs_python_commit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)
        lrl._disable("prior fault")
        calls: list[Path] = []
        lrl.rust_atomic_commit(
            tmp_path / "t",
            tmp_path / "d",
            None,
            python_commit=lambda t, _d, _c: calls.append(t),
        )
        assert calls == [tmp_path / "t"]

    def test_unresolvable_crate_runs_python_commit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        calls: list[Path] = []
        lrl.rust_atomic_commit(
            tmp_path / "t",
            tmp_path / "d",
            None,
            python_commit=lambda t, _d, _c: calls.append(t),
        )
        assert calls == [tmp_path / "t"]
        assert lrl.rust_locks_disabled() is True

    def test_engaged_commit_passes_str_paths_and_chmod(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)
        seen: list[tuple[str, str, int | None]] = []

        def fake_commit(tmp: str, dest: str, chmod: int | None) -> None:
            seen.append((tmp, dest, chmod))

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        lrl.rust_atomic_commit(
            tmp_path / "t.tmp",
            tmp_path / "d.parquet",
            0o600,
            python_commit=lambda *_a: pytest.fail("oracle must not run"),
        )
        assert seen == [(str(tmp_path / "t.tmp"), str(tmp_path / "d.parquet"), 0o600)]

    def test_enospc_propagates_raw(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise OSError(errno.ENOSPC, "disk full")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        with pytest.raises(OSError) as exc_info:
            lrl.rust_atomic_commit(
                tmp_path / "t",
                tmp_path / "d",
                None,
                python_commit=lambda *_a: pytest.fail("oracle must not run"),
            )
        assert exc_info.value.errno == errno.ENOSPC
        assert lrl.rust_locks_disabled() is False  # the guard owns the taxonomy

    def test_crate_fault_finishes_the_same_commit_via_oracle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise RuntimeError("panic-mapped fault")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        committed: list[tuple[Path, Path, int | None]] = []
        with capture_logs() as logs:
            lrl.rust_atomic_commit(
                tmp_path / "t",
                tmp_path / "d",
                None,
                python_commit=lambda t, d, c: committed.append((t, d, c)),
            )
        assert committed == [(tmp_path / "t", tmp_path / "d", None)]
        assert lrl.rust_locks_disabled() is True
        assert any(e["event"] == "lake_rust_locks.locks_autoreverted" for e in logs)

    def test_panic_belt_finishes_the_same_commit_via_oracle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)
        panic = _install_panic_home(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise panic("boom")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        committed: list[Path] = []
        lrl.rust_atomic_commit(
            tmp_path / "t",
            tmp_path / "d",
            None,
            python_commit=lambda t, _d, _c: committed.append(t),
        )
        assert committed == [tmp_path / "t"]
        assert lrl.rust_locks_disabled() is True


class TestAtomicIoRoutingSeam:
    """`_atomic_write_core` end-to-end through the seam — both arms."""

    def test_off_arm_bomb_crate_never_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Flag OFF (default): the crate is never resolved, bytes land via Python."""
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        monkeypatch.setattr(
            lrl,
            "_resolve_crate",
            lambda: pytest.fail("OFF arm must never resolve the crate"),
        )
        dest = tmp_path / "off.parquet"
        atomic_write_via(dest, lambda t: t.write_bytes(b"python arm"))
        assert dest.read_bytes() == b"python arm"
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_on_arm_routes_commit_through_crate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Flag ON: the fake crate performs the commit; bytes land at dest."""
        _enable(monkeypatch)
        crate_calls: list[str] = []

        def fake_commit(tmp: str, dest: str, chmod: int | None) -> None:
            crate_calls.append(tmp)
            atomic_io._python_commit_replace(Path(tmp), Path(dest), chmod)

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        dest = tmp_path / "on.parquet"
        atomic_write_via(dest, lambda t: t.write_bytes(b"routed"))
        assert dest.read_bytes() == b"routed"
        assert len(crate_calls) == 1
        assert crate_calls[0].startswith(str(tmp_path / ".brutex-write-"))  # prefix law
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_on_arm_chmod_reaches_the_crate_and_dest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _enable(monkeypatch)

        def fake_commit(tmp: str, dest: str, chmod: int | None) -> None:
            atomic_io._python_commit_replace(Path(tmp), Path(dest), chmod)

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        dest = tmp_path / "secret.json"
        atomic_write_simple(dest, lambda t: t.write_text("s"), chmod=0o600)
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600

    def test_on_arm_crate_enospc_maps_to_lake_write_halt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fault injection at the facade seam: the EXISTING guard owns the taxonomy."""
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise OSError(errno.ENOSPC, "disk full")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        dest = tmp_path / "full.parquet"
        with pytest.raises(LakeWriteHalt) as exc_info:
            atomic_write_via(dest, lambda t: t.write_bytes(b"x"))
        assert exc_info.value.reason == "disk_full"
        assert not dest.exists()
        assert list(tmp_path.glob(".brutex-write-*")) == []  # cleanup on any error

    def test_on_arm_crate_enospc_stays_raw_for_simple(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The guard-FREE variant keeps raw errno for its callers' own mapping."""
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise OSError(errno.ENOSPC, "disk full")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        with pytest.raises(OSError) as exc_info:
            atomic_write_simple(tmp_path / "s.json", lambda t: t.write_text("x"))
        assert exc_info.value.errno == errno.ENOSPC
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_on_arm_crate_eacces_is_not_mis_escalated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-disk-full errno through the ON arm stays RAW under the guard."""
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise OSError(errno.EACCES, "permission denied")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        with pytest.raises(PermissionError):
            atomic_write_via(tmp_path / "d.parquet", lambda t: t.write_bytes(b"x"))
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_on_arm_memory_error_in_write_to_maps_to_oom_halt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A Python-side OOM (pre-crossing) maps identically on the ON arm."""
        _enable(monkeypatch)
        _fake_crate(monkeypatch)  # bomb crate — write_to raises BEFORE the commit

        def write_to(_t: Path) -> None:
            raise MemoryError

        with pytest.raises(LakeWriteHalt) as exc_info:
            atomic_write_via(tmp_path / "oom.parquet", write_to)
        assert exc_info.value.reason == "out_of_memory"
        assert list(tmp_path.glob(".brutex-write-*")) == []

    def test_on_arm_mkstemp_enospc_still_maps_before_the_crate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Temp creation stays Python on BOTH arms — the mkstemp guard law holds ON."""
        _enable(monkeypatch)
        _fake_crate(monkeypatch)  # bomb crate — must never be reached
        import tempfile

        def full_disk_mkstemp(*_a: object, **_k: object) -> tuple[int, str]:
            raise OSError(errno.ENOSPC, "no space")

        monkeypatch.setattr(tempfile, "mkstemp", full_disk_mkstemp)
        with pytest.raises(LakeWriteHalt) as exc_info:
            atomic_write_via(tmp_path / "d.parquet", lambda t: t.write_bytes(b"x"))
        assert exc_info.value.reason == "disk_full"

    def test_on_arm_crate_fault_degrades_and_bytes_still_land(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A crate fault mid-commit: the oracle finishes the SAME temp's commit."""
        _enable(monkeypatch)

        def fake_commit(*_a: object) -> None:
            raise RuntimeError("brutex-lake atomic_commit_replace panicked")

        _fake_crate(monkeypatch, atomic_commit_replace=fake_commit)
        dest = tmp_path / "degraded.parquet"
        with capture_logs() as logs:
            atomic_write_via(dest, lambda t: t.write_bytes(b"still lands"))
        assert dest.read_bytes() == b"still lands"
        assert lrl.rust_locks_disabled() is True
        assert any(e["event"] == "lake_rust_locks.locks_autoreverted" for e in logs)
        assert list(tmp_path.glob(".brutex-write-*")) == []


class TestPartitionLockRoutingSeam:
    """`partition_write_lock` end-to-end through the seam — both arms."""

    def test_off_arm_bomb_crate_never_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(lrl.LAKE_RUST_LOCKS_ENV, raising=False)
        monkeypatch.setattr(
            lrl,
            "_resolve_crate",
            lambda: pytest.fail("OFF arm must never resolve the crate"),
        )
        dest = tmp_path / "bars" / "2024-01.parquet"
        with partition_write_lock(tmp_path, dest):
            assert partition_lock_path(tmp_path, dest).exists()

    def test_on_arm_routes_attempt_and_release_fd_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ON arm: the crate sees ONLY ints (fd) — the path law stays Python-owned."""
        _enable(monkeypatch)
        attempts: list[object] = []
        releases: list[object] = []
        import fcntl as _fcntl

        def fake_try(fd: object) -> bool:
            attempts.append(fd)
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)  # type: ignore[arg-type]
            return True

        def fake_release(fd: object) -> None:
            releases.append(fd)
            os.close(fd)  # type: ignore[arg-type]

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try, lock_release=fake_release)
        dest = tmp_path / "bars" / "2024-02.parquet"
        opened: list[str] = []
        real_open = os.open

        def spy_open(path: object, *a: object, **k: object) -> int:
            opened.append(str(path))
            return real_open(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(partition_lock.os, "open", spy_open)
        with partition_write_lock(tmp_path, dest):
            pass
        assert attempts and releases and attempts == releases  # same fd end-to-end
        assert all(isinstance(fd, int) for fd in attempts)
        # SAME-PATH pin: the fd the crate locked was opened on the byte-exact
        # Python-derived sidecar path — Rust never re-derives the path law.
        assert str(partition_lock_path(tmp_path, dest)) in opened

    def test_on_arm_timeout_parity_message_and_type(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A never-acquiring ON arm raises today's PartitionLockTimeout bytes."""
        _enable(monkeypatch)
        _fake_crate(monkeypatch, lock_try_exclusive=lambda _fd: False)
        beats: list[int] = []
        monkeypatch.setattr(partition_lock, "emit_progress_beat", lambda: beats.append(1))
        fd = os.open(tmp_path / "held.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            clock_values = iter([0.0, 0.05, 0.11])
            with pytest.raises(PartitionLockTimeout) as exc_info:
                partition_lock._acquire_bounded(
                    fd,
                    timeout_seconds=0.1,
                    clock=lambda: next(clock_values),
                    sleep=lambda _s: None,
                )
        finally:
            os.close(fd)
        assert beats  # the liveness beat law holds on the ON arm
        assert str(exc_info.value) == str(PartitionLockTimeout(0.1))

    def test_on_arm_mid_loop_degrade_converges_to_python_flock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A crate fault mid-acquire: the SAME loop finishes via the real flock."""
        _enable(monkeypatch)

        def fake_try(_fd: int) -> bool:
            raise RuntimeError("fault on first attempt")

        _fake_crate(monkeypatch, lock_try_exclusive=fake_try)
        dest = tmp_path / "bars" / "2024-03.parquet"
        with capture_logs(), partition_write_lock(tmp_path, dest):
            # Acquired via the Python oracle after the latch — prove the
            # flock is genuinely HELD: a second OFD must be refused.
            probe = os.open(partition_lock_path(tmp_path, dest), os.O_RDWR)
            try:
                assert partition_lock._flock_attempt(probe) is False
            finally:
                os.close(probe)
        assert lrl.rust_locks_disabled() is True

    def test_python_flock_attempt_oracle_semantics(self, tmp_path: Path) -> None:
        """The extracted oracle: True on acquire, False while a peer holds."""
        path = tmp_path / "probe.lock"
        holder = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        waiter = os.open(path, os.O_RDWR)
        try:
            assert partition_lock._flock_attempt(holder) is True
            assert partition_lock._flock_attempt(waiter) is False
        finally:
            os.close(holder)  # kernel releases the flock
            assert partition_lock._flock_attempt(waiter) is True
            os.close(waiter)

    def test_python_flock_attempt_real_oserror_propagates(self, tmp_path: Path) -> None:
        """A dead (already-closed) fd surfaces its real errno raw — never folded."""
        fd = os.open(tmp_path / "dead.lock", os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with pytest.raises(OSError) as exc_info:
            partition_lock._flock_attempt(fd)
        assert exc_info.value.errno == errno.EBADF


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    A routing leaf shipped WITHOUT its weekly mutation wiring would repeat
    the DEC-LAKE-STATS-MANIFEST-001 round-8 refuter class (an unwired gate
    presented as a deferred one — Golden Rule #10). The module:suite pair
    sits in the workflow's 3-field ``module:testfile:timeout`` loop with the
    EMPTY third field; this deterministic pin (the
    ``TestMutationWorkflowWiring`` precedent) fails if the pair is ever
    dropped. HONEST: it pins the WIRING only — the weekly run executes on
    its own schedule/push triggers.
    """

    def test_pair_is_wired(self) -> None:
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert '"brutex/data/lake_rust_locks.py:tests/unit/test_lake_rust_locks.py:"' in text, (
            "lake_rust_locks.py is not wired into mutation.yml's per-module mutmut loop"
        )

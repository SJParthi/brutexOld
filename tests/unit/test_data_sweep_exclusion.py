"""Tests for `brutex.data.sweep_exclusion` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).

100% line+branch over the sweep-vs-pull exclusion flock: real ``fcntl.flock``
locks in tmp dirs (R4/G10 both-ways refusal, extreme test 9), the WRITE-FREE
probe proof (dir listing before/after — an absent lock file is NEVER
created), SH-coexist (concurrent sweeps), the TOCTOU both-refuse race arm
(pull acquired between probe and re-probe via an injected hook), the
monkeypatched ``os.open`` OSError arms (a probe ``PermissionError``
propagates loudly; an ENOSPC on the guard's create maps to ``LakeWriteHalt``
via ``lake_write_guard``), and the drift pins vs
``brutex.data.partition_lock`` / ``brutex.adapters.groww.pull_singleflight``
(tests may import adapters; the module must not — AST-pinned).
"""

from __future__ import annotations

import ast
import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.adapters.groww import pull_singleflight as psf
from brutex.data import partition_lock as plk
from brutex.data import sweep_exclusion
from brutex.data.derive_lock import derive_lock_path, derive_write_lock
from brutex.data.errors import LakeWriteHalt
from brutex.data.sweep_exclusion import (
    PULL_SINGLEFLIGHT_LOCK_NAME,
    SWEEP_LOCK_FILENAME,
    PullActiveError,
    SweepRunningError,
    check_pull_exclusion,
    pull_in_progress,
    pull_lock_path,
    sweep_in_progress,
    sweep_lock_path,
    sweep_run_guard,
)
from brutex.models import BruteXError
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator


@contextmanager
def _hold_exclusive(lock_path: Path) -> Generator[None, None, None]:
    """Hold a real ``LOCK_EX`` on *lock_path* — the shape every real pull uses."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _listing(root: Path) -> list[str]:
    """Recursive relative-path listing — the write-free-probe oracle."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class TestLockPaths:
    def test_sweep_lock_path_shape(self, tmp_path: Path) -> None:
        assert sweep_lock_path(tmp_path) == tmp_path / ".locks" / "sweep.lock"

    def test_pull_lock_path_matches_adapter_derivation(self, tmp_path: Path) -> None:
        """The restated pull-lock path is byte-equal to the adapter's own."""
        assert pull_lock_path(tmp_path) == psf.pull_singleflight_lock_path(tmp_path)

    def test_paths_never_created(self, tmp_path: Path) -> None:
        sweep_lock_path(tmp_path)
        pull_lock_path(tmp_path)
        assert _listing(tmp_path) == []


class TestDriftPins:
    """The restated-per-module constants can never drift (BUILD-SPEC §10.3)."""

    def test_lock_subdir_matches_partition_lock(self) -> None:
        assert (
            sweep_exclusion._LOCK_SUBDIR  # pyright: ignore[reportPrivateUsage]
            == plk._LOCK_SUBDIR  # pyright: ignore[reportPrivateUsage]
        )

    def test_pull_lock_name_matches_pull_singleflight(self) -> None:
        assert PULL_SINGLEFLIGHT_LOCK_NAME == psf.PULL_SINGLEFLIGHT_LOCK_NAME

    def test_sweep_lock_filename_value(self) -> None:
        assert SWEEP_LOCK_FILENAME == "sweep.lock"

    def test_module_never_imports_the_writing_probe_or_adapters(self) -> None:
        """AST pin: no adapter / pull_intent import — the layering law + the
        write-free doctrine (``probe_pull_singleflight`` WRITES)."""
        source = Path(sweep_exclusion.__file__).read_text(encoding="utf-8")
        modules: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        assert not any(m.startswith("brutex.adapters") for m in modules)
        assert not any("pull_intent" in m for m in modules)


class TestWriteFreeProbes:
    def test_absent_pull_lock_reads_false_without_creating(self, tmp_path: Path) -> None:
        assert pull_in_progress(tmp_path) is False
        assert _listing(tmp_path) == []  # not even .locks/ was created

    def test_absent_sweep_lock_reads_false_without_creating(self, tmp_path: Path) -> None:
        assert sweep_in_progress(tmp_path) is False
        assert _listing(tmp_path) == []

    def test_existing_unheld_locks_read_false_and_stay_untouched(self, tmp_path: Path) -> None:
        for path in (pull_lock_path(tmp_path), sweep_lock_path(tmp_path)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        before = _listing(tmp_path)
        assert pull_in_progress(tmp_path) is False
        assert sweep_in_progress(tmp_path) is False
        assert _listing(tmp_path) == before

    def test_held_pull_lock_reads_true(self, tmp_path: Path) -> None:
        with _hold_exclusive(pull_lock_path(tmp_path)):
            assert pull_in_progress(tmp_path) is True
        assert pull_in_progress(tmp_path) is False  # kernel-released on fd close

    def test_probe_oserror_propagates_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable ``.locks`` is never a silent ``False``."""
        real_open = os.open

        def deny(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith(".lock"):
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", deny)
        with pytest.raises(PermissionError):
            pull_in_progress(tmp_path)
        with pytest.raises(PermissionError):
            sweep_in_progress(tmp_path)


class TestCheckPullExclusion:
    def test_quiet_pass_when_no_sweep(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            check_pull_exclusion(tmp_path)
        assert logs == []

    def test_raises_sweep_running_with_log_when_sweep_holds(self, tmp_path: Path) -> None:
        with (
            sweep_run_guard(tmp_path),
            capture_logs() as logs,
            pytest.raises(SweepRunningError) as excinfo,
        ):
            check_pull_exclusion(tmp_path)
        assert excinfo.value.lock_path == sweep_lock_path(tmp_path)
        assert "sweep-vs-pull exclusion" in str(excinfo.value)
        assert "re-run after the sweep completes" in str(excinfo.value)
        events = [e for e in logs if e["event"] == "pull.sweep_exclusion_refused"]
        assert len(events) == 1
        assert events[0]["lock_path"] == str(sweep_lock_path(tmp_path))


class TestSweepRunGuard:
    def test_happy_path_holds_sh_for_the_block_and_releases(self, tmp_path: Path) -> None:
        assert sweep_in_progress(tmp_path) is False
        with sweep_run_guard(tmp_path):
            assert sweep_in_progress(tmp_path) is True
            mode = sweep_lock_path(tmp_path).stat().st_mode & 0o777
            assert mode == 0o600
        assert sweep_in_progress(tmp_path) is False

    def test_concurrent_sweeps_sh_coexist(self, tmp_path: Path) -> None:
        """LOCK_SH is shared — G6 single-flight deliberately NOT smuggled in."""
        with sweep_run_guard(tmp_path), sweep_run_guard(tmp_path):
            assert sweep_in_progress(tmp_path) is True
        assert sweep_in_progress(tmp_path) is False

    def test_r4_g10_live_pull_lock_refuses_sweep_and_vice_versa(self, tmp_path: Path) -> None:
        """Extreme test 9 (unit half): a real held pull LOCK_EX refuses the
        sweep PRE-write; a held sweep SH refuses the pull-side check."""
        with _hold_exclusive(pull_lock_path(tmp_path)):
            with (
                capture_logs() as logs,
                pytest.raises(PullActiveError) as excinfo,
                sweep_run_guard(tmp_path),
            ):
                pytest.fail("guard must refuse while a pull runs")
            # ZERO writes on this refusal path — not even sweep.lock.
            assert not sweep_lock_path(tmp_path).exists()
            assert excinfo.value.lock_path == pull_lock_path(tmp_path)
            assert "lock wins" in str(excinfo.value)
            assert "watch /universe" in str(excinfo.value)
            refused = [e for e in logs if e["event"] == "sweep.preflight.lock_refused"]
            assert [e["holder"] for e in refused] == ["pull_in_progress"]
        # …and vice versa: sweep first ⇒ the pull side refuses.
        with sweep_run_guard(tmp_path), pytest.raises(SweepRunningError):
            check_pull_exclusion(tmp_path)

    def test_derive_in_flight_refuses_pre_write(self, tmp_path: Path) -> None:
        with (
            derive_write_lock(tmp_path),
            capture_logs() as logs,
            pytest.raises(PullActiveError) as excinfo,
            sweep_run_guard(tmp_path),
        ):
            pytest.fail("guard must refuse while a derive runs")
        assert not sweep_lock_path(tmp_path).exists()
        assert excinfo.value.lock_path == derive_lock_path(tmp_path)
        refused = [e for e in logs if e["event"] == "sweep.preflight.lock_refused"]
        assert [e["holder"] for e in refused] == ["derive_in_progress"]

    def test_momentary_exclusive_probe_bounces_sh_fail_closed(self, tmp_path: Path) -> None:
        """A peer's momentary EX probe on sweep.lock ⇒ PullActiveError, not a wait."""
        with (
            _hold_exclusive(sweep_lock_path(tmp_path)),
            capture_logs() as logs,
            pytest.raises(PullActiveError) as excinfo,
            sweep_run_guard(tmp_path),
        ):
            pytest.fail("shared acquire must fail closed")
        assert excinfo.value.lock_path == sweep_lock_path(tmp_path)
        refused = [e for e in logs if e["event"] == "sweep.preflight.lock_refused"]
        assert [e["holder"] for e in refused] == ["sweep_lock_contended"]
        # The fd was released — a later launch succeeds cleanly.
        with sweep_run_guard(tmp_path):
            assert sweep_in_progress(tmp_path) is True

    def test_toctou_pull_starting_mid_launch_both_sides_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extreme test 9 TOCTOU arm: a pull acquiring between the guard's probe
        and RE-probe ends with BOTH sides refusing (fail-closed, no double-run)."""
        real_probe = sweep_exclusion.pull_in_progress
        calls = {"n": 0}

        def racing_probe(root: Path) -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                # The racing pull grabs its LOCK_EX right before the RE-probe…
                path = pull_lock_path(root)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # …and ITS post-acquire re-probe refuses too (the sweep
                    # already holds SH at this instant) — the pull side of
                    # "both refuse".
                    with pytest.raises(SweepRunningError):
                        check_pull_exclusion(root)
                    # The sweep's RE-probe runs while the EX lock is LIVE…
                    return real_probe(root)
                finally:
                    # …and the fd is ALWAYS released (the flock dies with it).
                    os.close(fd)
            return real_probe(root)

        monkeypatch.setattr(sweep_exclusion, "pull_in_progress", racing_probe)
        with pytest.raises(PullActiveError), sweep_run_guard(tmp_path):
            pytest.fail("the sweep side must refuse on the RE-probe")
        assert calls["n"] == 2  # pre-probe + re-probe — both ran
        # The guard released its SH on the refusal path.
        assert sweep_in_progress(tmp_path) is False

    def test_enospc_on_lock_create_maps_to_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The create path runs under lake_write_guard — a full disk halts gracefully."""
        real_open = os.open

        def full_disk(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith(SWEEP_LOCK_FILENAME):
                raise OSError(errno.ENOSPC, "no space left on device", str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", full_disk)
        with pytest.raises(LakeWriteHalt), sweep_run_guard(tmp_path):
            pytest.fail("create must halt on ENOSPC")


class TestErrorTypes:
    def test_both_errors_are_brutex_errors_carrying_lock_path(self, tmp_path: Path) -> None:
        pull_err = PullActiveError(tmp_path / "x.lock")
        sweep_err = SweepRunningError(tmp_path / "y.lock")
        assert isinstance(pull_err, BruteXError)
        assert isinstance(sweep_err, BruteXError)
        assert pull_err.lock_path == tmp_path / "x.lock"
        assert sweep_err.lock_path == tmp_path / "y.lock"

"""Tests for `brutex.engine.sweep_singleflight` (DEFECT 1a — CLI sweep single-flight).

100% line+branch: the lock-path shape, the held → second-acquire-refused contract
(a SEPARATE fd in the same process raises :class:`SweepAlreadyRunningError` under
the shared non-blocking flock core), that releasing lets it re-acquire, and the
error carrying the contended lock path. Mirrors ``test_pull_singleflight`` — the
sweep lock is a thin translating wrapper over the SAME
:func:`brutex.adapters.groww.pull_singleflight.flock_singleflight` core.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.engine.sweep_singleflight import (
    SWEEP_SINGLEFLIGHT_LOCK_NAME,
    SweepAlreadyRunningError,
    sweep_singleflight_lock,
    sweep_singleflight_lock_path,
)


def test_lock_path_is_stable_sidecar_under_out_root(tmp_path: Path) -> None:
    path = sweep_singleflight_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / SWEEP_SINGLEFLIGHT_LOCK_NAME
    # Computed only — never created by the path helper.
    assert not path.exists()


def test_acquire_creates_locks_dir_and_lockfile(tmp_path: Path) -> None:
    with sweep_singleflight_lock(tmp_path):
        lock_path = sweep_singleflight_lock_path(tmp_path)
        assert lock_path.exists()


def test_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    # Acquire once; a SECOND acquisition (a SEPARATE fd, same process) must be
    # refused immediately by the non-blocking flock — the cross-process guard's
    # core contract. The pull-flavoured error is translated to the sweep one.
    with (
        sweep_singleflight_lock(tmp_path),
        pytest.raises(SweepAlreadyRunningError) as excinfo,
        sweep_singleflight_lock(tmp_path),  # pragma: no cover — __enter__ raises first
    ):
        pass  # pragma: no cover — body never runs (the inner acquire raises)
    # The error names the contended lock file for an actionable operator message.
    assert excinfo.value.lock_path == sweep_singleflight_lock_path(tmp_path)
    assert "another sweep is already running" in str(excinfo.value)


def test_release_lets_it_reacquire(tmp_path: Path) -> None:
    # First holder releases on ``with`` exit; a fresh acquire then succeeds.
    with sweep_singleflight_lock(tmp_path):
        pass
    with sweep_singleflight_lock(tmp_path):
        pass  # re-acquired without raising — the release worked.


def test_body_exception_propagates_unchanged_and_releases(tmp_path: Path) -> None:
    # A body error (raised while the lock is HELD) must propagate as itself — the
    # PullAlreadyRunningError → SweepAlreadyRunningError translation is scoped to
    # the ACQUIRE alone, never a body raise — and the lock must still release.
    sentinel = ValueError("boom in the with-body")
    with pytest.raises(ValueError) as excinfo, sweep_singleflight_lock(tmp_path):
        raise sentinel
    assert excinfo.value is sentinel
    # Released despite the body error — a fresh acquire succeeds.
    with sweep_singleflight_lock(tmp_path):
        pass


def test_error_carries_the_lock_path_and_custom_message() -> None:
    lock_path = Path("/some/root/.locks/sweep_all.lock")
    # Default message composes the refusal wording.
    err = SweepAlreadyRunningError(lock_path)
    assert err.lock_path == lock_path
    assert str(lock_path) in str(err)
    assert "another sweep is already running" in str(err)
    # An explicit message overrides the default.
    err2 = SweepAlreadyRunningError(lock_path, "custom wording")
    assert str(err2) == "custom wording"
    assert err2.lock_path == lock_path

"""Unit tests for :mod:`brutex.wipe` — the total-wipe core.

Safety-critical (it deletes real data), so the guards are exercised against a
REAL filesystem (tmp dirs + real symlinks) rather than mocks, plus injected
fakes for the partial-failure branch. Covers 100% line + branch, including the
hostile override matrix (``BRUTEX_LAKE_ROOT=$HOME`` etc. must be REFUSED).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.wipe import (
    OVERRIDE_ENV_VARS,
    UnsafeWipeError,
    WipeTarget,
    assert_safe,
    execute_wipe,
    server_is_running,
    wipe_targets,
)

# ── wipe_targets ─────────────────────────────────────────────────────────


def test_wipe_targets_home_only_without_cwd(tmp_path: Path) -> None:
    """With no cwd + no overrides, the only target is ``$HOME/.brutex``."""
    targets = wipe_targets({}, home=tmp_path)
    assert [t.path for t in targets] == [tmp_path / ".brutex"]


def test_wipe_targets_adds_in_repo_logs_when_cwd_given(tmp_path: Path) -> None:
    """With a cwd, the in-repo ``./.brutex-logs`` is added as a candidate."""
    cwd = tmp_path / "repo"
    targets = wipe_targets({}, home=tmp_path, cwd=cwd)
    assert [t.path for t in targets] == [tmp_path / ".brutex", cwd / ".brutex-logs"]


def test_wipe_targets_adds_outside_home_override(tmp_path: Path) -> None:
    """An override resolving OUTSIDE the home dir is added."""
    external = tmp_path / "external" / "rt.json"
    targets = wipe_targets({"BRUTEX_RUNTIME_FILE": str(external)}, home=tmp_path)
    assert external in [t.path for t in targets]


def test_wipe_targets_skips_override_inside_home(tmp_path: Path) -> None:
    """An override INSIDE the home dir is skipped (removing home covers it)."""
    inside = tmp_path / ".brutex" / "logs"
    targets = wipe_targets({"BRUTEX_LOG_DIR": str(inside)}, home=tmp_path)
    assert [t.path for t in targets] == [tmp_path / ".brutex"]


def test_wipe_targets_skips_override_equal_to_home(tmp_path: Path) -> None:
    """An override pointing exactly AT the home dir is skipped (already covered)."""
    targets = wipe_targets({"BRUTEX_LOG_DIR": str(tmp_path / ".brutex")}, home=tmp_path)
    assert [t.path for t in targets] == [tmp_path / ".brutex"]


def test_wipe_targets_dedupes_same_outside_path(tmp_path: Path) -> None:
    """Two overrides pointing at the same outside path are added only once."""
    shared = str(tmp_path / "external")
    targets = wipe_targets({"BRUTEX_LOG_DIR": shared, "BRUTEX_AUDIT_PATH": shared}, home=tmp_path)
    paths = [t.path for t in targets]
    assert paths.count(Path(shared)) == 1
    assert len(paths) == 2  # home + the single deduped external


def test_wipe_targets_registry_covers_all_overrides() -> None:
    """The override registry includes every BRUTEX_* persistent location."""
    assert OVERRIDE_ENV_VARS == (
        "BRUTEX_LAKE_ROOT",
        "BRUTEX_CREDENTIALS_FILE",
        "BRUTEX_GROWW_TOKEN_PATH",
        "BRUTEX_LOG_DIR",
        "BRUTEX_LOG_FILE",
        "BRUTEX_RUNTIME_FILE",
        "BRUTEX_AUDIT_PATH",
        "BRUTEX_LIVE_SUBJECTS_FILE",
    )


# ── assert_safe (the strict allowlist) ───────────────────────────────────


def test_assert_safe_allows_brutex_home(tmp_path: Path) -> None:
    """The BruteX home dir itself is a safe, deep, non-system leaf."""
    home = tmp_path / "a" / "home"
    cwd = tmp_path / "b" / "repo"
    assert_safe(home / ".brutex", home=home, cwd=cwd)  # no raise


def test_assert_safe_refuses_symlink(tmp_path: Path) -> None:
    """A symlinked target is refused outright (never delete through a link)."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(UnsafeWipeError, match="symlink"):
        assert_safe(link, home=tmp_path / "home", cwd=tmp_path / "repo")


def test_assert_safe_refuses_root(tmp_path: Path) -> None:
    """The filesystem root (too shallow) is refused."""
    with pytest.raises(UnsafeWipeError, match="root or too shallow"):
        assert_safe(Path("/"), home=tmp_path / "home", cwd=tmp_path / "repo")


def test_assert_safe_refuses_home_itself(tmp_path: Path) -> None:
    """``$HOME`` itself is refused (would nuke the whole home dir)."""
    home = tmp_path / "a" / "home"
    with pytest.raises(UnsafeWipeError, match="home directory"):
        assert_safe(home, home=home, cwd=tmp_path / "b" / "repo")


def test_assert_safe_refuses_home_ancestor(tmp_path: Path) -> None:
    """An ancestor of ``$HOME`` is refused."""
    home = tmp_path / "a" / "home"
    with pytest.raises(UnsafeWipeError, match="home directory or an ancestor"):
        assert_safe(home.parent, home=home, cwd=tmp_path / "z" / "repo")


def test_assert_safe_refuses_cwd_itself(tmp_path: Path) -> None:
    """The working directory / repo is refused."""
    home = tmp_path / "a" / "home"
    cwd = tmp_path / "b" / "repo"
    with pytest.raises(UnsafeWipeError, match="working directory / repo"):
        assert_safe(cwd, home=home, cwd=cwd)


def test_assert_safe_refuses_cwd_ancestor(tmp_path: Path) -> None:
    """An ancestor of the repo is refused (would delete the repo)."""
    home = tmp_path / "a" / "home"
    cwd = tmp_path / "b" / "repo"
    with pytest.raises(UnsafeWipeError, match="working directory / repo or an ancestor"):
        assert_safe(cwd.parent, home=home, cwd=cwd)


def test_assert_safe_allows_external_deep_leaf(tmp_path: Path) -> None:
    """A deep, non-system path unrelated to home/cwd is allowed."""
    assert_safe(
        tmp_path / "data" / "mylake",
        home=tmp_path / "a" / "home",
        cwd=tmp_path / "b" / "repo",
    )  # no raise


# ── execute_wipe ─────────────────────────────────────────────────────────


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    """Return isolated (home, cwd) trees for execute_wipe tests."""
    return tmp_path / "a" / "home", tmp_path / "b" / "repo"


def test_execute_wipe_real_dir_file_and_absent(tmp_path: Path) -> None:
    """Default primitives: a dir is rmtree'd, a file unlinked, a missing one absent."""
    home, cwd = _trees(tmp_path)
    a_dir = tmp_path / "data" / "lake"
    a_dir.mkdir(parents=True)
    (a_dir / "inner.txt").write_text("x")
    a_file = tmp_path / "data" / "creds.json"
    a_file.write_text("{}")
    missing = tmp_path / "data" / "gone"
    targets = [
        WipeTarget(a_dir, "lake"),
        WipeTarget(a_file, "creds"),
        WipeTarget(missing, "absent"),
    ]
    report = execute_wipe(targets, home=home, cwd=cwd, dry_run=False)
    assert set(report.removed) == {a_dir, a_file}
    assert report.absent == (missing,)
    assert report.failed == ()
    assert not a_dir.exists()
    assert not a_file.exists()


def test_execute_wipe_dry_run_deletes_nothing(tmp_path: Path) -> None:
    """Dry-run reports would-remove targets but leaves them on disk."""
    home, cwd = _trees(tmp_path)
    a_dir = tmp_path / "data" / "lake"
    a_dir.mkdir(parents=True)
    report = execute_wipe([WipeTarget(a_dir, "lake")], home=home, cwd=cwd, dry_run=True)
    assert report.removed == (a_dir,)
    assert report.dry_run is True
    assert a_dir.exists()  # untouched


def test_execute_wipe_refuses_unsafe_target(tmp_path: Path) -> None:
    """An unsafe (symlink) target is recorded as failed, not deleted."""
    home, cwd = _trees(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    report = execute_wipe([WipeTarget(link, "link")], home=home, cwd=cwd, dry_run=False)
    assert report.removed == ()
    assert len(report.failed) == 1
    assert report.failed[0][0] == link
    assert real.exists()  # the symlink target is untouched


def test_execute_wipe_captures_delete_error(tmp_path: Path) -> None:
    """An ``OSError`` during deletion is captured in ``failed``, run continues."""
    home, cwd = _trees(tmp_path)
    a_dir = tmp_path / "data" / "lake"
    a_dir.mkdir(parents=True)

    def _boom(_path: Path) -> None:
        raise OSError("device busy")

    report = execute_wipe(
        [WipeTarget(a_dir, "lake")], home=home, cwd=cwd, dry_run=False, rmtree=_boom
    )
    assert report.removed == ()
    assert report.failed[0][0] == a_dir
    assert "device busy" in report.failed[0][1]


def test_execute_wipe_hostile_override_home_is_refused(tmp_path: Path) -> None:
    """End-to-end: ``BRUTEX_LAKE_ROOT=$HOME`` is enumerated then REFUSED, never deleted."""
    home = tmp_path / "a" / "home"
    home.mkdir(parents=True)
    cwd = tmp_path / "b" / "repo"
    targets = wipe_targets({"BRUTEX_LAKE_ROOT": str(home)}, home=home)
    report = execute_wipe(targets, home=home, cwd=cwd, dry_run=True)
    assert home in [p for p, _ in report.failed]
    assert home.exists()


# ── server_is_running ────────────────────────────────────────────────────


def test_server_is_running_no_runtime() -> None:
    """No runtime file → not running."""
    assert server_is_running(None, pid_alive=lambda _pid: True) is False


def test_server_is_running_live_pid() -> None:
    """A recorded pid that is alive → running."""
    assert server_is_running(4321, pid_alive=lambda pid: pid == 4321) is True


def test_server_is_running_dead_pid() -> None:
    """A recorded pid that is dead → not running."""
    assert server_is_running(4321, pid_alive=lambda _pid: False) is False

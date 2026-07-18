"""Real-git integration tests for the box-update leg (DEC-BOX-AUTODEPLOY-LANE-001 §10).

Harness: real ``subprocess`` git against ``tmp_path`` repos with a bare
"origin" remote (the ``test_ci_bugfix_reversion`` precedent — no network); a
HYBRID runner routes git argv to the REAL default runner and every python/
maturin/pip child to a scripted response, keeping the doctor/wheel surface
deterministic while the git state machine is genuine.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from brutex.nightly import box_update as bu
from brutex.nightly.box_update import (
    BoxUpdateStatus,
    default_command_runner,
    run_box_update,
)

_PASS_CHECKS = [
    ["wheel_freshness", "PASS", "ok"],
    ["wheel_freshness_fno_math", "PASS", "ok"],
    ["wheel_source_digest", "PASS", "ok"],
    ["wheel_source_digest_fno_math", "PASS", "ok"],
]


def _gate_stdout(checks: Sequence[Sequence[str]] = _PASS_CHECKS) -> str:
    return f"{bu._IMPORT_OK_MARKER}\n{bu._CHECKS_MARKER}{json.dumps(list(checks))}\n"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # nosec B603 — fixed test argv
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _tree_hash(root: Path) -> str:
    """Whole-tree content hash oracle (the nightly restore-drill idiom)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class HybridRunner:
    """Real git; scripted python/maturin/pip children; full argv census."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.gate_responses: list[tuple[int, str, str]] = []
        self.maturin_response: tuple[int, str, str] = (0, "", "")
        self.maturin_effect: Any = None  # callable(cwd) run before responding
        self.smoke_response: tuple[int, str, str] = (0, "", "")

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        call = [str(a) for a in argv]
        self.calls.append(call)
        if call[0] == "git":
            return default_command_runner(call, cwd=cwd, timeout_s=timeout_s, env=env)
        if call[0].endswith("maturin"):
            if self.maturin_effect is not None:
                self.maturin_effect(cwd)
            return self.maturin_response
        if call[0].endswith("pip"):
            return (0, "", "")
        if call[0] == sys.executable and call[1] == "-c" and "brutex.doctor" in call[2]:
            if self.gate_responses:
                return self.gate_responses.pop(0)
            return (0, _gate_stdout(), "")
        if call[0] == sys.executable and call[1] == "-c":
            return self.smoke_response
        raise AssertionError(f"unscripted argv: {call}")

    def maturin_calls(self) -> list[list[str]]:
        return [a for a in self.calls if a[0].endswith("maturin")]

    def gate_calls(self) -> list[list[str]]:
        return [
            a
            for a in self.calls
            if a[0] == sys.executable and a[1] == "-c" and "brutex.doctor" in a[2]
        ]


@pytest.fixture
def repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone on ``main`` + its bare origin, with a committed lake shape."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=main", ".")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    lock = work / "rust" / "fno-math" / "Cargo.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("# committed lock v1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "push", "-q", "origin", "main")
    return work, origin


def _advance_origin(work: Path, origin: Path, rel: str, content: str) -> str:
    """Land one commit on origin/main WITHOUT moving the work checkout."""
    stage = work.parent / "stage"
    if not stage.exists():
        subprocess.run(  # nosec B603
            ["git", "clone", "-q", str(origin), str(stage)], check=True, capture_output=True
        )
        _git(stage, "config", "user.email", "test@example.com")
        _git(stage, "config", "user.name", "Test")
    else:
        _git(stage, "pull", "-q", "origin", "main")
    dest = stage / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    _git(stage, "add", "-A")
    _git(stage, "commit", "-q", "-m", f"advance {rel}")
    _git(stage, "push", "-q", "origin", "main")
    return _git(stage, "rev-parse", "HEAD")


def _run(work: Path, runner: HybridRunner, *, state_dir: Path | None = None) -> bu.BoxUpdateOutcome:
    return run_box_update(
        repo_root=work,
        runner=runner,
        env={},
        state_dir=state_dir,
        sleep=lambda _s: None,
    )


class TestRealGit:
    def test_ff_only_happy_path_python_only(self, repo_pair: tuple[Path, Path]) -> None:
        # §10 test 4 — origin advances (python-only) ⇒ merged, no maturin,
        # gate invoked once, status=updated.
        work, origin = repo_pair
        to_sha = _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        runner = HybridRunner()
        out = _run(work, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert out.from_sha != to_sha
        assert out.to_sha == to_sha
        assert _git(work, "rev-parse", "HEAD") == to_sha
        assert (work / "src" / "app.py").read_text(encoding="utf-8") == "VERSION = 2\n"
        assert runner.maturin_calls() == []
        assert len(runner.gate_calls()) == 1
        assert out.delta_rust is False and out.delta_pyproject is False

    def test_idempotent_rerun_is_up_to_date(self, repo_pair: tuple[Path, Path]) -> None:
        # §10 test 8 — the second run: zero merge/build argv, UP_TO_DATE.
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        runner = HybridRunner()
        assert _run(work, runner).status is BoxUpdateStatus.UPDATED
        runner2 = HybridRunner()
        out = _run(work, runner2)
        assert out.status is BoxUpdateStatus.UP_TO_DATE
        assert out.from_sha == out.to_sha == out.running_sha
        assert not any(a[:2] == ["git", "merge"] for a in runner2.calls)
        assert runner2.maturin_calls() == []

    def test_dirty_tree_skip_leaves_everything_untouched(
        self, repo_pair: tuple[Path, Path]
    ) -> None:
        # §10 test 2 — tracked modification ⇒ dirty_tree; tree byte-identical.
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        (work / "src" / "app.py").write_text("LOCAL EDIT\n", encoding="utf-8")
        head_before = _git(work, "rev-parse", "HEAD")
        tree_before = _tree_hash(work)
        out = _run(work, HybridRunner())
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "dirty_tree"
        assert _git(work, "rev-parse", "HEAD") == head_before
        assert _tree_hash(work) == tree_before

    def test_diverged_local_commit_skips_and_survives(self, repo_pair: tuple[Path, Path]) -> None:
        # §10 test 3 — a local commit on main ⇒ diverged; commit reachable.
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        (work / "src" / "local.py").write_text("x = 1\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "local work")
        local_sha = _git(work, "rev-parse", "HEAD")
        out = _run(work, HybridRunner())
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "diverged"
        assert _git(work, "rev-parse", "HEAD") == local_sha

    def test_untracked_only_proceeds_and_scratch_survives(
        self, repo_pair: tuple[Path, Path]
    ) -> None:
        # §10 test 26 — a stray non-ignored ?? file never blocks; byte-identical.
        work, origin = repo_pair
        to_sha = _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        scratch = work / "scratch.txt"
        scratch.write_text("operator scratch\n", encoding="utf-8")
        out = _run(work, HybridRunner())
        assert out.status is BoxUpdateStatus.UPDATED
        assert _git(work, "rev-parse", "HEAD") == to_sha
        assert scratch.read_text(encoding="utf-8") == "operator scratch\n"
        assert "untracked_files=1" in out.detail

    def test_gate_failure_rolls_back_tree_byte_identical(
        self, repo_pair: tuple[Path, Path]
    ) -> None:
        # §10 test 12 — merge applied, gate import_failed ⇒ rollback: the tree
        # returns byte-identical to pre-merge (whole-tree hash oracle).
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        head_before = _git(work, "rev-parse", "HEAD")
        tree_before = _tree_hash(work)
        scratch = work / "scratch.txt"
        scratch.write_text("survives\n", encoding="utf-8")
        runner = HybridRunner()
        runner.gate_responses = [(1, "", "ModuleNotFoundError: boom")]
        out = _run(work, runner)
        assert out.status is BoxUpdateStatus.ROLLED_BACK
        assert out.gate == "import_failed"
        assert _git(work, "rev-parse", "HEAD") == head_before
        assert scratch.read_text(encoding="utf-8") == "survives\n"
        scratch.unlink()
        assert _tree_hash(work) == tree_before
        assert out.running_sha == head_before

    def test_rollback_refused_when_tracked_file_dirtied_mid_window(
        self, repo_pair: tuple[Path, Path]
    ) -> None:
        # §10 test 12 second arm — a concurrent writer dirties a tracked file
        # mid-run ⇒ checkout -B refuses ⇒ status=error; content preserved.
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        runner = HybridRunner()

        def _dirty_then_fail(_cwd: Path | None) -> None:
            (work / "src" / "app.py").write_text("CONCURRENT EDIT\n", encoding="utf-8")

        _advance_origin(work, origin, "rust/fno-math/src/lib.rs", "// rust\n")
        runner.maturin_effect = _dirty_then_fail
        runner.maturin_response = (1, "", "build boom")  # required failure ⇒ rollback
        out = _run(work, runner)
        assert out.status is BoxUpdateStatus.ERROR
        assert (work / "src" / "app.py").read_text(encoding="utf-8") == "CONCURRENT EDIT\n"

    def test_lane_lock_rewrite_restored_and_marker_cleared(
        self, repo_pair: tuple[Path, Path], tmp_path: Path
    ) -> None:
        # §10 test 6b integration — a build step genuinely rewrites the tracked
        # Cargo.lock ⇒ post-build the tree is CLEAN, the marker cleared, and a
        # night-2 G5 does NOT skip dirty_tree.
        work, origin = repo_pair
        state = tmp_path / "state"
        _advance_origin(work, origin, "rust/fno-math/src/lib.rs", "// rust v2\n")
        committed = (work / "rust/fno-math/Cargo.lock").read_bytes()
        runner = HybridRunner()

        def _rewrite_lock(cwd: Path | None) -> None:
            # Only the fno-math build regenerates ITS lock (the real shape).
            if cwd == work / "rust/fno-math":
                (work / "rust/fno-math/Cargo.lock").write_text(
                    "# REGENERATED by bare build\n", encoding="utf-8"
                )

        runner.maturin_effect = _rewrite_lock
        out = _run(work, runner, state_dir=state)
        assert out.status is BoxUpdateStatus.UPDATED
        assert "rust/fno-math/Cargo.lock" in out.lock_restored
        assert (work / "rust/fno-math/Cargo.lock").read_bytes() == committed
        assert _git(work, "status", "--porcelain") == ""  # tree clean
        assert not (state / "build_marker.json").exists()
        # The quarantine preserved the regenerated bytes (recoverable).
        copies = list((state / "quarantine").rglob("rust__fno-math__Cargo.lock"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == b"# REGENERATED by bare build\n"
        # Night 2: no dirty_tree skip.
        out2 = _run(work, HybridRunner(), state_dir=state)
        assert out2.status is BoxUpdateStatus.UP_TO_DATE

    def test_crash_residue_prologue_restore_quiet_upstream(
        self, repo_pair: tuple[Path, Path], tmp_path: Path
    ) -> None:
        # §10 tests 6b/20(c) round-9 arm — planted marker + dirty lock with
        # origin UNMOVED ⇒ the pre-G4a prologue restores + clears, the night
        # ends up_to_date with lock_restored non-empty (fails-before-round-9).
        work, _origin = repo_pair
        state = tmp_path / "state"
        state.mkdir()
        (work / "rust/fno-math/Cargo.lock").write_text("# crash residue\n", encoding="utf-8")
        (state / "build_marker.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "lock_paths": ["rust/fno-math/Cargo.lock"],
                    "started_utc": "2026-07-17T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        out = _run(work, HybridRunner(), state_dir=state)
        assert out.status is BoxUpdateStatus.UP_TO_DATE
        assert out.lock_restored == ("rust/fno-math/Cargo.lock",)
        assert out.quarantined == ("rust/fno-math/Cargo.lock",)
        assert (work / "rust/fno-math/Cargo.lock").read_text(encoding="utf-8") == (
            "# committed lock v1\n"
        )
        assert not (state / "build_marker.json").exists()
        assert _git(work, "status", "--porcelain") == ""

    def test_operator_dirty_lock_no_marker_never_clobbered(
        self, repo_pair: tuple[Path, Path], tmp_path: Path
    ) -> None:
        # §10 test 20(b) — the never-clobber invariant on REAL git.
        work, origin = repo_pair
        _advance_origin(work, origin, "src/app.py", "VERSION = 2\n")
        lock = work / "rust/fno-math/Cargo.lock"
        lock.write_text("# DELIBERATE OPERATOR EDIT\n", encoding="utf-8")
        out = _run(work, HybridRunner(), state_dir=tmp_path / "state")
        assert out.status is BoxUpdateStatus.SKIPPED
        assert out.skip_reason == "dirty_tree"
        assert lock.read_text(encoding="utf-8") == "# DELIBERATE OPERATOR EDIT\n"
        assert out.lock_restored == () and out.quarantined == ()

    def test_rust_delta_builds_and_updates(self, repo_pair: tuple[Path, Path]) -> None:
        # §10 test 5 integration shape — a rust delta triggers the WHEEL_SET
        # builds with real git supplying the diff.
        work, origin = repo_pair
        _advance_origin(work, origin, "rust/trading-core/src/lib.rs", "// core\n")
        runner = HybridRunner()
        out = _run(work, runner)
        assert out.status is BoxUpdateStatus.UPDATED
        assert out.delta_rust is True
        assert len(runner.maturin_calls()) == len(bu.WHEEL_SET)

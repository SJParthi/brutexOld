"""Fast pre-push smoke test — ~30s sanity check before pushing to origin.

Phase 1 Week 1 scaffolding deliverable per ``docs/TRACK1_PHASE_PLAN.md``.

This module is the **fastest meaningful CI subset** an operator can
run locally before pushing. It exists to catch the noisy-flake class
of failures (lint, format, basic imports, conventional-commit subject)
in seconds rather than waiting 10+ minutes for the full CI suite.

## Contract

* All tests in this file MUST complete in well under 30 seconds.
* Tests MUST NOT require network access, the Groww SDK, or any
  optional dependency. The smoke test runs on a fresh checkout with
  only the default Python toolchain.
* Tests assert on the **environment + repository invariants**, not on
  the BruteX logic — the full unit + property suites cover that.

## What this catches

* Stray Python syntax errors that ruff missed locally
* Unformatted code (``ruff format --check`` regressions)
* Missing ``__init__.py`` in a new test directory
* Broken core package import (``from brutex import ...``)
* PR commit subject that violates the conventional-commits regex

What this does NOT catch (delegated to full CI):

* Test failures, type errors, mutation-score regressions
* Coverage drops, benchmark regressions
* Anything requiring the Rust ``fno_math`` extension

## Decision

DEC-PHASE1-SMOKE-AND-SKELETONS-001 (MIGRATION entry #251).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONV_CHECKER = REPO_ROOT / "scripts" / "ci" / "check-conventional-commits.sh"


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` and return the completed process; never raise on non-zero."""
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        timeout=30,
    )


# ────────────────────────────────────────────────────────────────────
# Repository layout invariants
# ────────────────────────────────────────────────────────────────────


class TestRepoLayout:
    def test_top_level_paths_exist(self) -> None:
        for name in ("brutex", "tests", "docs", "pyproject.toml", "CLAUDE.md"):
            assert (REPO_ROOT / name).exists(), f"Missing repo artifact: {name}"

    def test_every_test_dir_has_init(self) -> None:
        for sub in ("unit", "integration", "property", "golden", "bench", "stress", "smoke"):
            init = REPO_ROOT / "tests" / sub / "__init__.py"
            assert init.is_file(), f"Missing tests/{sub}/__init__.py"

    def test_no_pycache_in_git_tracked_dirs(self) -> None:
        # Caches are gitignored; their presence in git ls-files would be a real bug.
        result = _run(["git", "ls-files", "**/__pycache__/**"])
        assert result.returncode == 0
        assert result.stdout.strip() == "", (
            f"git is tracking __pycache__ entries: {result.stdout!r}"
        )


# ────────────────────────────────────────────────────────────────────
# Core package import smoke
# ────────────────────────────────────────────────────────────────────


class TestImportSmoke:
    def test_brutex_top_level_imports(self) -> None:
        # If this raises, the package is broken in a way no unit test would
        # catch (the unit suite imports specific submodules; this one imports
        # the top-level shim).
        mod = importlib.import_module("brutex")
        assert mod is not None

    def test_brutex_vocabulary_imports(self) -> None:
        # Vocabulary is the hot-path import every strategy worker hits.
        mod = importlib.import_module("brutex.vocabulary")
        assert hasattr(mod, "evaluate_atoms")

    def test_brutex_indicators_imports(self) -> None:
        mod = importlib.import_module("brutex.indicators")
        assert mod is not None


# ────────────────────────────────────────────────────────────────────
# Lint + format checks (runs ruff against the working tree)
# ────────────────────────────────────────────────────────────────────


class TestLintFormat:
    def test_ruff_check_clean(self) -> None:
        result = _run(["ruff", "check", "brutex", "tests"])
        assert result.returncode == 0, (
            f"ruff check failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_ruff_format_check_clean(self) -> None:
        result = _run(["ruff", "format", "--check", "brutex", "tests"])
        assert result.returncode == 0, (
            f"ruff format --check failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# ────────────────────────────────────────────────────────────────────
# Conventional commit subject (last commit on the working tree)
# ────────────────────────────────────────────────────────────────────


def _subject_is_conventional(subject: str) -> bool:
    """Delegate to the ONE shell validator (scripts/ci/check-conventional-commits.sh).

    Writes the subject as the first line of a temp file and runs the checker in
    --msg-file mode: exit 0 => valid (or a Merge* subject), nonzero => invalid.
    This is the single source of truth (Golden Rule #11 COMMON) — no second regex.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(subject + "\n")
        msg_path = fh.name
    try:
        result = subprocess.run(
            ["bash", str(_CONV_CHECKER), "--msg-file", msg_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    finally:
        os.unlink(msg_path)
    return result.returncode == 0


class TestConventionalCommit:
    def test_head_commit_subject_matches(self) -> None:
        if not _CONV_CHECKER.exists():
            pytest.skip("conventional-commits checker not present")
        result = _run(["git", "log", "-1", "--format=%s"])
        if result.returncode != 0:
            pytest.skip(f"git log failed: {result.stderr}")
        subject = result.stdout.strip()
        if not subject:
            pytest.skip("no commits on this branch yet")
        # Merge commits are auto-generated and exempt from Conventional
        # Commits (exactly as commitlint's defaultIgnores treats them). On PR
        # CI, actions/checkout's shallow clone of the `refs/pull/N/merge` ref
        # makes the merge commit a graft boundary, so `--format=%P` reports
        # ZERO parents — a parent-count check is unreliable. The subject
        # prefix is the robust signal: GitHub writes "Merge <sha> into <sha>".
        if subject.startswith("Merge "):
            pytest.skip("HEAD is a merge commit — exempt from Conventional Commits")
        assert _subject_is_conventional(subject), (
            "HEAD subject not Conventional-Commits compliant per the shared "
            f"shell validator: {subject!r}"
        )

    @pytest.mark.skipif(
        not _CONV_CHECKER.exists(), reason="conventional-commits checker not present"
    )
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            # VALID — comma multi-scope is accepted (parity with the reference
            # parser used by commitlint + Release Please); this is the exact
            # drift class that red-ed main on the #1380 comma-scope squash.
            ("fix(dashboard,test): make it hermetic", True),
            ("feat(a,b,c): multi scope", True),
            ("fix(options): single scope", True),
            ("chore: no scope", True),
            ("feat(engine)!: breaking bang", True),
            ("refactor(a/b-c_d): charset ok", True),
            # INVALID — empty/compound/malformed forms stay rejected.
            ("fix(): empty scope", False),
            ("fix(options,): trailing empty segment", False),
            ("fix(,calendars): leading empty segment", False),
            ("ci+chore: compound type", False),
            ("nope: bad type", False),
            ("fix scope no colon", False),
            ("fix(UPPER): uppercase scope", False),
        ],
    )
    def test_msg_file_delegation_policy(self, subject: str, expected: bool) -> None:
        """The delegation itself has HEAD-independent teeth: comma multi-scope
        is accepted and empty/compound/malformed forms are rejected, all through
        the shared ``--msg-file`` validator (Golden Rule #11 COMMON, no 2nd regex).
        """
        assert _subject_is_conventional(subject) is expected


# ────────────────────────────────────────────────────────────────────
# Required CLAUDE.md / MIGRATION.md docs
# ────────────────────────────────────────────────────────────────────


class TestPrePushHookWired:
    """DEC-PRE-PUSH-HOOK-001 — the pre-push fast gate must be *wired*, not
    just present as a pytest module. These assert the three wiring points so
    a regression that silently un-installs the hook is caught.
    """

    def test_pre_push_script_exists_and_is_valid_bash(self) -> None:
        script = REPO_ROOT / "scripts" / "hooks" / "pre-push.sh"
        assert script.is_file(), "scripts/hooks/pre-push.sh is missing"
        # `bash -n` parses without executing — catches syntax errors.
        result = _run(["bash", "-n", str(script)])
        assert result.returncode == 0, f"pre-push.sh has a bash syntax error: {result.stderr}"

    def test_precommit_config_declares_pre_push_stage(self) -> None:
        cfg = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        # The pre-push hook must reference the script AND declare the stage.
        assert "scripts/hooks/pre-push.sh" in cfg, (
            "pre-commit config does not call the pre-push script"
        )
        assert "stages: [pre-push]" in cfg, "pre-commit config declares no pre-push stage"

    def test_bootstrap_installs_pre_push_hook(self) -> None:
        boot = (REPO_ROOT / "scripts" / "setup" / "bootstrap.sh").read_text(encoding="utf-8")
        assert "--hook-type pre-push" in boot, (
            "bootstrap.sh does not install the pre-push hook stage"
        )


class TestRequiredDocs:
    def test_claude_md_present(self) -> None:
        path = REPO_ROOT / "CLAUDE.md"
        assert path.is_file()
        # Cheap sanity: file is non-trivial (> 1 KB) — guards against an empty stub.
        assert path.stat().st_size > 1024

    def test_migration_md_present_and_grows(self) -> None:
        path = REPO_ROOT / "docs" / "MIGRATION.md"
        assert path.is_file()
        # Sanity: file is large (>> 1 KB) — guards against accidental truncation.
        assert path.stat().st_size > 10_000

    def test_track1_phase_plan_present(self) -> None:
        path = REPO_ROOT / "docs" / "TRACK1_PHASE_PLAN.md"
        assert path.is_file()

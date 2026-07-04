"""Verify the conventional-commits checker actually rejects bad prefixes.

Per docs/STANDARDS_100PCT.md §1 row 5: every commit must follow
Conventional Commits. The bash checker at scripts/ci/check-conventional-commits.sh
is supposed to enforce this.

This test PROVES it works by feeding the checker a known-bad commit
message and asserting non-zero exit.

This was a real bypass discovered by Agent D: my own commit
`ci+chore:` was rejected by this checker but landed anyway because
the pre-commit hook only checks HEAD~1, not the full PR range.

If this test FAILS, contributors can land any commit message.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.redteam.conftest import REPO_ROOT


def _checker_path() -> Path:
    return REPO_ROOT / "scripts" / "ci" / "check-conventional-commits.sh"


@pytest.mark.skipif(not _checker_path().exists(), reason="conventional-commits checker not present")
def test_checker_rejects_compound_prefix(tmp_path: Path) -> None:
    """The checker MUST reject `ci+chore:` (compound prefix).

    Compound prefixes are not in the allowlist:
    feat, fix, docs, style, refactor, perf, test, chore, build, ci, revert.

    The verifier creates a throwaway git repo, makes a commit with the
    bad message, then invokes the checker against HEAD.
    """
    # Create throwaway git repo with TWO commits — the checker validates
    # HEAD~1..HEAD by default, so we need a baseline commit before the bad one.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "redteam",
        "GIT_AUTHOR_EMAIL": "rt@x",
        "GIT_COMMITTER_NAME": "redteam",
        "GIT_COMMITTER_EMAIL": "rt@x",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, env=env)

    # Commit 1: baseline with VALID conventional-commit prefix
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: baseline commit for redteam fixture"],
        cwd=tmp_path,
        check=True,
        env=env,
    )

    # Commit 2: the planted BAD compound prefix
    (tmp_path / "f.txt").write_text("hello v2", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "ci+chore: plant a known-bad compound prefix"],
        cwd=tmp_path,
        check=True,
        env=env,
    )

    # Now HEAD~1..HEAD covers exactly the bad commit. Run the checker.
    result = subprocess.run(
        ["bash", str(_checker_path())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode != 0, (
        f"RED-TEAM FAILURE: conventional-commits checker did NOT reject 'ci+chore:'.\n"
        f"This is exactly the bypass Agent D found.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(not _checker_path().exists(), reason="conventional-commits checker not present")
@pytest.mark.parametrize(
    ("subject", "expect_ok"),
    [
        # Comma-separated multi-scope is ACCEPTED — parity with the
        # reference parser (conventional-commits-parser, used by
        # commitlint + Release Please), which parses it as one scope
        # token. Published commits are never amended/force-pushed
        # (CLAUDE.md §3), so the checker must accept ecosystem-valid
        # subjects (regression: PR #1085 commit 0a86a18).
        ("fix(options,calendars): comma-separated multi-scope is valid", True),
        ("feat(a,b,c): three comma-separated scope segments", True),
        # The teeth stay in: empty scope, empty segment, compound type.
        ("fix(): empty scope stays rejected", False),
        ("fix(options,): trailing empty scope segment stays rejected", False),
        ("fix(,calendars): leading empty scope segment stays rejected", False),
        ("ci+chore: compound type stays rejected", False),
    ],
)
def test_checker_scope_comma_policy(tmp_path: Path, subject: str, expect_ok: bool) -> None:
    """Comma multi-scope accepted; empty/compound forms still rejected."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "redteam",
        "GIT_AUTHOR_EMAIL": "rt@x",
        "GIT_COMMITTER_NAME": "redteam",
        "GIT_COMMITTER_EMAIL": "rt@x",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, env=env)

    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: baseline commit for redteam fixture"],
        cwd=tmp_path,
        check=True,
        env=env,
    )

    (tmp_path / "f.txt").write_text("hello v2", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", subject],
        cwd=tmp_path,
        check=True,
        env=env,
    )

    result = subprocess.run(
        ["bash", str(_checker_path())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    if expect_ok:
        assert result.returncode == 0, (
            f"checker REJECTED a commitlint-valid subject {subject!r}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    else:
        assert result.returncode != 0, (
            f"checker ACCEPTED a malformed subject {subject!r} — the gate lost its teeth.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

"""Subprocess pins for the bug-fix fix-reversion gate (STANDARDS row 10 hardening).

DEC-TEST-APPARATUS-DEBTS-001(c): ``scripts/ci/validate_bugfix_reversion.py``
must block a fix:-titled PR whose shipped tests still pass with the modified
brutex source files reverted to merge-base (the MIGRATION.md #1154
surviving-regression-fixture class). These tests invoke the REAL script as
a subprocess (exactly as the CI job does — the test_ci_dec_id_validator.py
harness) against tmp_path ``git init`` fixture repos carrying a trivial
in-fixture package, so the whole file runs in seconds and never touches
the real brutex package.

Coverage scoping stated honestly: scripts/ci/ is OUTSIDE
``[tool.coverage.run] source=["brutex"]`` — these subprocess pins ARE the
enforcement, matching every sibling scanner.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_bugfix_reversion.py"

BUGGY_SOURCE = "def answer() -> int:\n    return 41\n"
FIXED_SOURCE = "def answer() -> int:\n    return 42\n"
REAL_TEST = (
    "from brutex.mod import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 42\n"
)
VACUOUS_TEST = "def test_vacuous() -> None:\n    assert True\n"
# Module-level sleep at merge-base: reverting re-introduces the hang, so the
# reversion pytest run must be timeout-killed (exit 124 = kill).
HANGING_SOURCE = "import time\n\ntime.sleep(300)\n\n\ndef answer() -> int:\n    return 41\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@test.invalid")
    _git(repo, "config", "user.name", "CI Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _fix_pr_repo(
    tmp_path: Path,
    *,
    base_source: str = BUGGY_SOURCE,
    head_source: str = FIXED_SOURCE,
    test_body: str = REAL_TEST,
    with_init: bool = True,
) -> Path:
    """A repo whose feature branch is a fix: PR — M brutex/mod.py + A test."""
    repo = _init_repo(tmp_path)
    if with_init:
        _write(repo, "brutex/__init__.py", "")
    _write(repo, "brutex/mod.py", base_source)
    _commit_all(repo, "base: buggy state")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, "brutex/mod.py", head_source)
    _write(repo, "tests/test_mod.py", test_body)
    _commit_all(repo, "fix: correct the answer")
    return repo


def _run_gate(
    repo: Path,
    *,
    title: str | None = "fix: correct the answer",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the REAL validator in *repo* with a controlled environment."""
    env = dict(os.environ)
    for key in (
        "PR_TITLE",
        "BASE_REF",
        "BUGFIX_REVERSION_ALLOW_SURVIVING",
        "PYTHONPATH",
        # The fixture repos have no conftest registering the repo's "ci"
        # hypothesis profile — keep the inner pytest runs decoupled.
        "HYPOTHESIS_PROFILE",
    ):
        env.pop(key, None)
    if title is not None:
        env["PR_TITLE"] = title
    env["BASE_REF"] = "main"
    # Short per-run budget so a fixture defect can never wedge the suite;
    # the hang test overrides it tighter still.
    env.setdefault("BUGFIX_REVERSION_TIMEOUT_S", "120")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_reverted_fix_is_killed(tmp_path: Path) -> None:
    """(a) real regression test fails on the reverted tree → gate passes."""
    repo = _fix_pr_repo(tmp_path)
    result = _run_gate(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reversion killed" in result.stdout


def test_surviving_tests_block(tmp_path: Path) -> None:
    """(b) vacuous shipped test passes with the fix reverted → exit 1."""
    repo = _fix_pr_repo(tmp_path, test_body=VACUOUS_TEST)
    result = _run_gate(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "MERGE BLOCKED" in result.stderr
    assert "ALL PASS with the fix reverted" in result.stderr


def test_escape_hatch_warns_and_continues(tmp_path: Path) -> None:
    """(c) BUGFIX_REVERSION_ALLOW_SURVIVING=1 → warn + exit 0, never silent."""
    repo = _fix_pr_repo(tmp_path, test_body=VACUOUS_TEST)
    result = _run_gate(repo, extra_env={"BUGFIX_REVERSION_ALLOW_SURVIVING": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BUGFIX_REVERSION_ALLOW_SURVIVING=1" in result.stderr


def test_added_only_source_skips(tmp_path: Path) -> None:
    """(d) A pure-addition fix (new module, no M brutex file) → skip exit 0."""
    repo = _init_repo(tmp_path)
    _write(repo, "brutex/__init__.py", "")
    _commit_all(repo, "base: empty package")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, "brutex/newmod.py", FIXED_SOURCE)
    _write(repo, "tests/test_newmod.py", VACUOUS_TEST)
    _commit_all(repo, "fix: add new module")
    result = _run_gate(repo, title="fix: add new module")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to revert" in result.stdout


def test_no_brutex_source_in_diff_skips(tmp_path: Path) -> None:
    """(e) docs-only + tests diff (no brutex source) → skip exit 0."""
    repo = _init_repo(tmp_path)
    _write(repo, "README.md", "v1\n")
    _commit_all(repo, "base: docs")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, "README.md", "v2\n")
    _write(repo, "tests/test_doc.py", VACUOUS_TEST)
    _commit_all(repo, "fix: docs typo")
    result = _run_gate(repo, title="fix: docs typo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to revert" in result.stdout


def test_non_fix_title_skips(tmp_path: Path) -> None:
    """(f) a feat:-titled PR never runs the reversion machinery."""
    repo = _fix_pr_repo(tmp_path, test_body=VACUOUS_TEST)
    result = _run_gate(repo, title="feat: not a bug fix")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not a bugfix title" in result.stdout


def test_unset_title_skips(tmp_path: Path) -> None:
    """(f) PR_TITLE unset (local/manual invocation) → skip exit 0."""
    repo = _fix_pr_repo(tmp_path, test_body=VACUOUS_TEST)
    result = _run_gate(repo, title=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PR_TITLE not set" in result.stdout


def test_no_shipped_tests_skips(tmp_path: Path) -> None:
    """Source-only fix diff → skip; the bugfix-regression-scan sibling owns it."""
    repo = _init_repo(tmp_path)
    _write(repo, "brutex/__init__.py", "")
    _write(repo, "brutex/mod.py", BUGGY_SOURCE)
    _commit_all(repo, "base: buggy state")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, "brutex/mod.py", FIXED_SOURCE)
    _commit_all(repo, "fix: source only")
    result = _run_gate(repo, title="fix: source only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bugfix-regression-scan sibling" in result.stdout


def test_baseline_red_is_exit_2(tmp_path: Path) -> None:
    """(g) shipped tests failing at HEAD = broken PR → exit 2, not a verdict."""
    failing_test = (
        "from brutex.mod import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 43\n"
    )
    repo = _fix_pr_repo(tmp_path, test_body=failing_test)
    result = _run_gate(repo)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "baseline pytest failed at HEAD" in result.stderr


def test_hang_on_reversion_counts_as_kill(tmp_path: Path) -> None:
    """(h) a reverted tree that HANGS is timeout-killed (124) and counts as a kill."""
    repo = _fix_pr_repo(tmp_path, base_source=HANGING_SOURCE)
    result = _run_gate(repo, extra_env={"BUGFIX_REVERSION_TIMEOUT_S": "15"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reversion killed" in result.stdout
    assert "124" in result.stdout


def test_invalid_timeout_env_is_exit_2(tmp_path: Path) -> None:
    """A malformed timeout override fails loudly, never a silent default."""
    repo = _fix_pr_repo(tmp_path)
    result = _run_gate(repo, extra_env={"BUGFIX_REVERSION_TIMEOUT_S": "soon"})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "BUGFIX_REVERSION_TIMEOUT_S" in result.stderr


def test_worktree_is_cleaned_up(tmp_path: Path) -> None:
    """(i) no leftover scratch worktrees after a run (kill AND block paths)."""
    scratch_tmp = tmp_path / "scratch-tmp"
    scratch_tmp.mkdir()

    kill_repo = _fix_pr_repo(tmp_path / "kill")
    result = _run_gate(kill_repo, extra_env={"TMPDIR": str(scratch_tmp)})
    assert result.returncode == 0, result.stdout + result.stderr

    block_repo = _fix_pr_repo(tmp_path / "block", test_body=VACUOUS_TEST)
    result = _run_gate(block_repo, extra_env={"TMPDIR": str(scratch_tmp)})
    assert result.returncode == 1, result.stdout + result.stderr

    assert list(scratch_tmp.iterdir()) == []
    for repo in (kill_repo, block_repo):
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        worktree_lines = [
            line for line in listing.stdout.splitlines() if line.startswith("worktree ")
        ]
        assert len(worktree_lines) == 1, listing.stdout


def test_shadowed_import_resolution_is_exit_2(tmp_path: Path) -> None:
    """(j) brutex resolving OUTSIDE the worktree → loud exit 2, never a verdict.

    Simulates the pip-editable shadowing class with PEP 420 precedence: the
    fixture repo ships brutex as a NAMESPACE package (no __init__.py), while
    a decoy dir on PYTHONPATH carries a REGULAR brutex package with the
    FIXED behavior. A regular package beats a namespace portion regardless
    of sys.path order, so the baseline passes against the decoy — and the
    reversion probe must detect that the "reverted" run would execute decoy
    (HEAD-equivalent) code and refuse with exit 2.
    """
    repo = _fix_pr_repo(tmp_path, with_init=False)
    decoy = tmp_path / "decoy"
    _write(decoy, "brutex/__init__.py", "")
    _write(decoy, "brutex/mod.py", FIXED_SOURCE)
    result = _run_gate(repo, extra_env={"PYTHONPATH": str(decoy)})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "reversion environment invalid" in result.stderr
    # The probe realpath-normalizes; compare against the resolved decoy path.
    assert str((decoy / "brutex").resolve()) in result.stderr

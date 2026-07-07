#!/usr/bin/env python3
"""Diff-scoped bug-fix fix-reversion gate — STANDARDS_100PCT.md §1 row 10 hardening.

The sibling ``validate_bugfix_regression.py`` enforces that a bug-fix PR
ships *some* test change. This gate enforces that the shipped tests
actually *detect* the bug: with the PR's modified ``brutex/`` source
files reverted to the merge-base (the pre-fix state), the PR's own test
files must FAIL. A regression fixture whose reverted-behavior mutant
survives (the DEC-SURFACE-TRIAL-001 / MIGRATION.md #1154 class — caught
there only by manual hostile review) is blocked mechanically.

Mechanism
---------
1. PR title must match the ONE common bug-fix definition
   (``BUGFIX_TITLE_PATTERN`` imported from the sibling — Golden Rule
   #11 COMMON); otherwise skip (exit 0).
2. ``base = git merge-base <base-ref> HEAD`` (tries ``origin/$BASE_REF``
   then ``$BASE_REF``). From ``git diff --name-status base..HEAD``:
   * reversion set = status-**M** ``brutex/**/*.py`` only (A-added files
     are excluded — reverting an added module is a trivial import-break
     kill, not evidence the test detects the bug);
   * shipped-test set = status A/M ``tests/**/test_*.py``.
   Either set empty → skip (exit 0) with the reason.
3. Baseline: the shipped tests must pass at HEAD (else exit 2 — a
   broken PR is not this gate's verdict).
4. Reversion: a scratch ``git worktree add --detach`` at HEAD (the main
   checkout is never mutated), then ``git checkout <base> -- <files>``
   inside it. Before ANY verdict is trusted, an import-resolution probe
   asserts ``brutex.__file__`` resolves INSIDE the worktree under the
   same ``PYTHONPATH``/cwd the reversion run uses — a pip-editable
   ``__editable__`` meta-path finder can silently shadow PYTHONPATH and
   make the "reverted" run execute HEAD code (fake-green/fake-block). If
   the probe fails: loud exit 2, never a silent verdict. Sanctioned
   alternatives if the PYTHONPATH layout loses in a given CI env:
   ``pip install -e`` the scratch worktree before the run, or uninstall
   the editable brutex in that job.
5. Verdict: ANY non-zero pytest exit on the reverted tree — including
   124, the coreutils ``timeout`` convention this script synthesizes on
   a hang (a hang IS a kill; the ci.yml mutmut-gate pattern) — means the
   tests detect the bug (exit 0). Pytest exit 0 on the reverted tree
   means the fix's own tests cannot see the fix → exit 1, merge blocked.

Usage
-----
::

    PR_TITLE='fix(...): ...' BASE_REF=main \
        python scripts/ci/validate_bugfix_reversion.py

Environment
-----------
``PR_TITLE``  — PR title (unset → skip; local/manual invocations stay green).
``BASE_REF``  — PR base branch (default ``main``).
``BUGFIX_REVERSION_TIMEOUT_S`` — per-pytest-run wall clock (default 600).
``BUGFIX_REVERSION_ALLOW_SURVIVING=1`` — warn-and-continue escape hatch,
exact twin of the sibling's ``BUGFIX_REGRESSION_ALLOW_MISSING``. CI
never sets it; use requires an ADR per the sibling convention.

Exit codes
----------
0 — skip (not a bug-fix PR / nothing to revert / no shipped tests) OR
    the reverted run killed the tests (gate satisfied).
1 — the shipped tests ALL PASS with the fix reverted (merge blocked).
2 — script/environment error: unresolvable base ref, git failure,
    baseline red at HEAD, invalid timeout, or the import-resolution
    probe failing (reversion environment invalid).

Structural false negatives (honest envelope per Golden Rule #10)
----------------------------------------------------------------
* **Rust-side fixes** are skipped entirely — reverting ``rust/**``
  requires a wheel rebuild per variant (a named follow-up, consistent
  with the ledgered still-timing-out Rust mutation jobs, MIGRATION.md
  #1511).
* **Pure-addition fixes** (new modules only, no M-status brutex file)
  have nothing to revert — skipped, stated in the skip message.
* **Whole-file reversion is coarser than per-hunk**: a PR mixing a fix
  with a refactor in one file reverts both, so the kill may come from
  the refactor, not the fix. Per-hunk ``git apply -R`` is a named v2
  ratchet (interdependent hunks produce non-importing intermediates).
* Renamed/copied files (git status R/C) are neither reverted nor
  counted as shipped tests — rename-heavy fix PRs fall back to the
  reviewer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# These gate scripts run BOTH as scripts (CI: python scripts/ci/<name>.py,
# where the script dir is sys.path[0]) AND as importlib-loaded MODULES
# (sibling-contract tests load them via spec_from_file_location, where
# it is not). Inject the script directory so the ONE bug-fix-PR title
# definition resolves in both modes (DEC-TEXTSCAN-COMMON-001 pattern).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_bugfix_regression import BUGFIX_TITLE_PATTERN

DEFAULT_TIMEOUT_S = 600
HANG_EXIT_CODE = 124  # coreutils `timeout` convention (the ci.yml mutmut-gate pattern)

# pytest exit-code classes, logged for diagnosis only — ANY non-zero
# exit on the reverted tree counts as a kill (a reverted module that no
# longer imports still proves the tests exercise the fixed code).
_PYTEST_EXIT_LABELS = {
    1: "tests failed (clean kill)",
    2: "execution interrupted / collection error",
    3: "internal pytest error",
    4: "pytest usage error",
    5: "no tests collected",
    HANG_EXIT_CODE: "hang killed by timeout (124 = coreutils timeout convention)",
}

# The import-resolution probe. argv[1] = the resolved scratch worktree
# root. Exit 0 iff `import brutex` resolves to a real file INSIDE it;
# any other outcome (shadowed by an editable install elsewhere, a
# namespace package with no __file__, an import failure) is exit != 0
# and the gate refuses to emit a verdict.
_IMPORT_PROBE = (
    "import os, sys\n"
    "import brutex\n"
    "path = os.path.realpath(brutex.__file__ or '')\n"
    "print(path)\n"
    "root = sys.argv[1]\n"
    "sys.exit(0 if path.startswith(root + os.sep) else 3)\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in *repo*, capturing output, never raising on rc."""
    return subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
        ["git", "-C", str(repo), *args],  # noqa: S607 — resolved via PATH like every sibling gate
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def resolve_timeout() -> int | None:
    """Per-pytest-run wall-clock budget; None on an invalid override."""
    raw = os.environ.get("BUGFIX_REVERSION_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_merge_base(repo: Path, base_ref: str) -> str | None:
    """Merge-base of the PR base ref and HEAD; tries origin/<ref> then <ref>."""
    for candidate in (f"origin/{base_ref}", base_ref):
        probe = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        if probe.returncode != 0:
            continue
        merge_base = _git(repo, "merge-base", candidate, "HEAD")
        if merge_base.returncode == 0:
            return merge_base.stdout.strip()
    return None


def changed_name_status(repo: Path, base: str) -> list[tuple[str, str]] | None:
    """(status, path) pairs from ``git diff --name-status base..HEAD``."""
    proc = _git(repo, "diff", "--name-status", f"{base}..HEAD")
    if proc.returncode != 0:
        print(f"❌ git diff failed: {proc.stderr.strip()}", file=sys.stderr)
        return None
    pairs: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            # Renames/copies (R100\told\tnew) carry two paths; taking the
            # LAST field keeps parsing uniform, and the status letter
            # filters them out below (documented false negative).
            pairs.append((parts[0], parts[-1]))
    return pairs


def is_reversion_source(status: str, path: str) -> bool:
    """Status-M brutex Python source only (added files excluded by design)."""
    return status == "M" and path.startswith("brutex/") and path.endswith(".py")


def is_shipped_test(status: str, path: str) -> bool:
    """Added-or-modified ``tests/**/test_*.py`` files shipped by the PR."""
    if status not in ("A", "M"):
        return False
    return (
        path.startswith("tests/") and path.endswith(".py") and Path(path).name.startswith("test_")
    )


def _pytest_env(pythonpath_root: Path) -> dict[str, str]:
    """Process env with *pythonpath_root* prepended to PYTHONPATH."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(pythonpath_root) + (os.pathsep + existing if existing else "")
    return env


def run_pytest(test_files: list[str], cwd: Path, timeout_s: int) -> int:
    """Run the shipped tests in *cwd*; a hang is reported as HANG_EXIT_CODE."""
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_files]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; output streams to the CI log
            cmd,
            cwd=str(cwd),
            env=_pytest_env(cwd),
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return HANG_EXIT_CODE
    return proc.returncode


def import_resolves_inside(worktree: Path, timeout_s: int) -> tuple[bool, str]:
    """MANDATORY guard: brutex must resolve INSIDE the scratch worktree.

    Runs under the SAME cwd + PYTHONPATH the reversion pytest run uses.
    Returns ``(ok, detail)`` where *detail* is the resolved path (or the
    probe's stderr on an import failure) for the loud exit-2 message.
    """
    root = str(worktree.resolve())
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", _IMPORT_PROBE, root],
            cwd=str(worktree),
            env=_pytest_env(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "import probe timed out"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    stdout = proc.stdout.strip()
    stderr_tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    return False, stdout or stderr_tail or "<no output>"


def _exit_label(rc: int) -> str:
    return _PYTEST_EXIT_LABELS.get(rc, "other non-zero exit")


def reversion_verdict(
    repo: Path,
    base: str,
    source_files: list[str],
    test_files: list[str],
    title: str,
    timeout_s: int,
) -> int:
    """Create the scratch worktree, revert, probe, run, and judge."""
    scratch_parent = Path(tempfile.mkdtemp(prefix="bugfix-reversion-"))
    worktree = scratch_parent / "worktree"
    try:
        added = _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        if added.returncode != 0:
            print(f"❌ git worktree add failed: {added.stderr.strip()}", file=sys.stderr)
            return 2

        # Probe BEFORE reverting: at this point the worktree is byte-equal
        # to HEAD (where the baseline just passed), so any probe failure is
        # an ENVIRONMENT defect (editable-install shadowing, broken
        # PYTHONPATH), never a consequence of the reverted code.
        ok, detail = import_resolves_inside(worktree, timeout_s)
        if not ok:
            print(
                f"❌ reversion environment invalid: brutex resolved to {detail} — "
                f"NOT inside the scratch worktree {worktree}. A pip-editable "
                "(__editable__ finder) install can shadow PYTHONPATH; the gate "
                "refuses to emit a verdict against the wrong tree. Fix the CI "
                "env (pip install -e the worktree, or uninstall the editable "
                "brutex in this job) — see the module docstring.",
                file=sys.stderr,
            )
            return 2

        reverted = _git(worktree, "checkout", base, "--", *source_files)
        if reverted.returncode != 0:
            print(
                f"❌ git checkout {base[:12]} -- <sources> failed: {reverted.stderr.strip()}",
                file=sys.stderr,
            )
            return 2
        print(
            f"→ reverted {len(source_files)} brutex source file(s) to merge-base "
            f"{base[:12]}: {', '.join(source_files[:10])}"
            f"{' …' if len(source_files) > 10 else ''}"
        )

        rc = run_pytest(test_files, cwd=worktree, timeout_s=timeout_s)
        if rc != 0:
            print(
                f"✅ reversion killed — pytest exit {rc} ({_exit_label(rc)}) with the "
                "fix reverted: the shipped regression tests detect the bug"
            )
            return 0

        if os.environ.get("BUGFIX_REVERSION_ALLOW_SURVIVING") == "1":
            print(
                f"⚠️  bug-fix PR '{title[:60]}' tests pass with the fix reverted "
                "[BUGFIX_REVERSION_ALLOW_SURVIVING=1, not failing — ADR required]",
                file=sys.stderr,
            )
            return 0

        print(
            f"❌ MERGE BLOCKED — PR '{title[:60]}' is a bug fix whose shipped tests "
            "ALL PASS with the fix reverted to merge-base — the regression test does "
            "not detect the bug; strengthen the test or split the PR.\n"
            "\n"
            f"Reverted files ({len(source_files)}): {', '.join(source_files)}\n"
            f"Shipped tests ({len(test_files)}): {', '.join(test_files)}\n"
            "\n"
            "Escape (legitimate exceptions only, ADR required, CI never sets it): "
            "BUGFIX_REVERSION_ALLOW_SURVIVING=1.\n"
            "See STANDARDS_100PCT.md §1 row 10 "
            "(scripts/ci/validate_bugfix_reversion.py).",
            file=sys.stderr,
        )
        return 1
    finally:
        # Cleanup runs on EVERY path (clean verdict, probe refusal, git
        # failure): unregister the linked worktree from the main repo,
        # then remove the scratch directory tree.
        _git(repo, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(scratch_parent, ignore_errors=True)


def main() -> int:
    """CLI entry point. Returns 0 pass/skip, 1 surviving-tests block, 2 error."""
    title = os.environ.get("PR_TITLE", "").strip()
    if not title:
        print(
            "ℹ️  PR_TITLE not set; skipping fix-reversion check (local/manual invocation)",
        )
        return 0
    if not BUGFIX_TITLE_PATTERN.match(title):
        print(
            f"ℹ️  skipped: not a bugfix title — '{title[:60]}' "
            "(fix:/bugfix:/hotfix: conventional-commit prefixes only)"
        )
        return 0

    timeout_s = resolve_timeout()
    if timeout_s is None:
        print(
            "❌ BUGFIX_REVERSION_TIMEOUT_S must be a positive integer",
            file=sys.stderr,
        )
        return 2

    toplevel = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        print(f"❌ not inside a git repository: {toplevel.stderr.strip()}", file=sys.stderr)
        return 2
    repo = Path(toplevel.stdout.strip())

    base_ref = os.environ.get("BASE_REF", "").strip() or "main"
    base = resolve_merge_base(repo, base_ref)
    if base is None:
        print(
            f"❌ cannot resolve base ref '{base_ref}' (tried origin/{base_ref} and "
            f"{base_ref}); is the checkout fetch-depth 0?",
            file=sys.stderr,
        )
        return 2

    pairs = changed_name_status(repo, base)
    if pairs is None:
        return 2
    source_files = [p for s, p in pairs if is_reversion_source(s, p)]
    test_files = [p for s, p in pairs if is_shipped_test(s, p)]

    if not source_files:
        print(
            "ℹ️  skipped: no modified brutex/**/*.py in the diff (docs/workflow/"
            "Rust-only or pure-addition fix — nothing to revert)"
        )
        return 0
    if not test_files:
        print(
            "ℹ️  skipped: no shipped tests/**/test_*.py in the diff — the "
            "bugfix-regression-scan sibling gate owns the missing-test failure"
        )
        return 0

    print(
        f"→ baseline: running {len(test_files)} shipped test file(s) at HEAD (timeout {timeout_s}s)"
    )
    baseline_rc = run_pytest(test_files, cwd=repo, timeout_s=timeout_s)
    if baseline_rc != 0:
        print(
            f"❌ baseline pytest failed at HEAD (exit {baseline_rc}: "
            f"{_exit_label(baseline_rc)}) — the PR itself is broken; not this "
            "gate's verdict",
            file=sys.stderr,
        )
        return 2

    return reversion_verdict(repo, base, source_files, test_files, title, timeout_s)


if __name__ == "__main__":
    sys.exit(main())

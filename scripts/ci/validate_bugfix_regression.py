#!/usr/bin/env python3
"""Bug-fix regression test scanner — partial automation of STANDARDS_100PCT.md §1 row 10.

Closes the meta-gap that "every bug-fix PR must include a regression
test" was 👁️ MANUAL with no automation. While full enforcement
(every fix needs the RIGHT kind of test for the bug) requires
reviewer judgement, this script catches the most common failure mode:

* PR title or body indicates a bug fix (``fix:`` / ``bug`` / ``regression``)
* but the PR diff does NOT touch ``tests/`` at all

Usage
-----
::

    python scripts/ci/validate_bugfix_regression.py [pr_body_file]

If ``pr_body_file`` is omitted, reads PR body from stdin (the CI
workflow pipes ``${{ github.event.pull_request.body }}`` in).
The PR title is read from env var ``PR_TITLE``. The list of changed
files is read from env var ``PR_FILES`` (newline-separated, output of
``git diff --name-only origin/main..HEAD`` in the workflow).

Exit codes
----------
0 — PR is not a bug fix (no automation needed) OR is a bug fix that
    touches tests/.
1 — bug-fix PR with no test file changes.
2 — script error (e.g., env vars missing).

Heuristic scope (honest envelope per Golden Rule #10)
------------------------------------------------------
This script does NOT enforce that every PR MUST be a bug fix or have
a test. It only catches the failure where a PR LOOKS LIKE a bug fix
(by title/body keywords) but ships no test changes. The "is this the
RIGHT regression test for the bug" judgement stays with the reviewer.

False-positive escape
---------------------
``BUGFIX_REGRESSION_ALLOW_MISSING=1`` reverts to warn-and-continue
for legitimate exceptions (e.g., bug fix in a generated artifact
that has no testable surface). CI never sets this; reviewer signs off.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Patterns that indicate a bug-fix PR.
# Title prefixes (conventional commits): "fix:", "fix(scope):", "bugfix:".
# Body keywords: "bug", "regression", "fixes #", "fixes:", "closes bug".
# Anchored with word boundaries where it matters so we don't match
# "prefix" or "configuration" by accident.
BUGFIX_TITLE_PATTERN = re.compile(
    r"^(fix|bugfix|hotfix)(\([^)]*\))?[:!]",
    re.IGNORECASE,
)
BUGFIX_BODY_PATTERN = re.compile(
    r"\b(bug|regression|fixes\s+#\d+|fixes:|hotfix)\b",
    re.IGNORECASE,
)


def is_bugfix_pr(title: str, body: str) -> bool:
    """Return True if the PR title or body signals a bug fix.

    Title check is anchored (must be a conventional-commit prefix);
    body check is keyword presence anywhere.
    """
    if BUGFIX_TITLE_PATTERN.match(title.strip()):
        return True
    return bool(BUGFIX_BODY_PATTERN.search(body))


def touches_tests(changed_files: list[str]) -> bool:
    """Return True if any changed file lives under ``tests/``."""
    return any(f.startswith("tests/") or "/tests/" in f for f in changed_files)


def read_pr_body(args: list[str]) -> str:
    """Read PR body from a file path (if given) or stdin."""
    if len(args) >= 2:
        return Path(args[1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on bug-fix-without-tests, 2 on script error."""
    argv = argv if argv is not None else sys.argv

    title = os.environ.get("PR_TITLE", "").strip()
    files_blob = os.environ.get("PR_FILES", "").strip()

    if not title:
        print(
            "ℹ️  PR_TITLE not set; skipping bug-fix scan (reviewer judgement)",
            file=sys.stderr,
        )
        return 0

    try:
        body = read_pr_body(argv)
    except OSError as exc:
        print(f"⚠️  could not read PR body: {exc}", file=sys.stderr)
        return 2

    if not is_bugfix_pr(title, body):
        print(f"ℹ️  PR title '{title[:60]}' is not a bug fix — no test requirement")
        return 0

    changed_files = [f for f in files_blob.splitlines() if f.strip()]

    if touches_tests(changed_files):
        print(
            f"✅ bug-fix PR '{title[:60]}' touches tests/ — regression test present"
        )
        return 0

    # Bug-fix without tests. Fail-closed unless override is set.
    if os.environ.get("BUGFIX_REGRESSION_ALLOW_MISSING") == "1":
        print(
            f"⚠️  bug-fix PR '{title[:60]}' has no test changes "
            "[BUGFIX_REGRESSION_ALLOW_MISSING=1, not failing]",
            file=sys.stderr,
        )
        return 0

    print(
        f"❌ MERGE BLOCKED — PR '{title[:60]}' looks like a bug fix but "
        "no files under tests/ were modified. Per STANDARDS_100PCT.md §1 "
        "row 10 (Bug-fix regression test): every bug fix MUST include a "
        "regression test that would have caught the bug.\n"
        "\n"
        "Fix: either (a) add a regression test under tests/, OR\n"
        "     (b) if the fix has no testable surface (e.g., a docs typo "
        "tagged as 'fix:'), rename the PR title to not use 'fix:'/'bugfix:'/"
        "'hotfix:' OR re-run with BUGFIX_REGRESSION_ALLOW_MISSING=1 + ADR.\n"
        "\n"
        "See STANDARDS_100PCT.md §1 row 10 (partial automation via "
        "scripts/ci/validate_bugfix_regression.py).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""DEC-ID validator — partial automation of STANDARDS_100PCT.md §1 row 18.

Closes the meta-gap that "every locked decision has a MIGRATION.md
entry" was 👁️ MANUAL with no automation. While full enforcement
(every concept change has a DEC-ID) requires reviewer judgement,
this script catches two common failure modes:

* EXISTENCE: PR body cites ``DEC-FOO-001`` but no entry exists in
  ``docs/MIGRATION.md`` for that ID.
* APPROPRIATENESS (DEC-TEST-APPARATUS-DEBTS-001(e)): a non-bot PR
  whose diff touches the behavioral surface (``brutex/**/*.py``,
  ``rust/**/*.rs``, ``.github/workflows/**`` — test files excluded via
  the COMMON ``touches_tests`` predicate imported from
  ``validate_bugfix_regression``) under a non-exempt conventional-
  commit title type (exempt set: ``docs``/``chore``/``test``/``style``/
  ``ci``) cites ZERO ledger-resolving DEC-IDs AND does not touch
  ``docs/MIGRATION.md``. Citation-OR-ledger-touch is the tightest
  achievable predicate — the multi-slice/amend house pattern
  legitimately cites a pre-existing DEC-ID without a fresh ledger
  entry; requiring a ledger touch would false-block it.

Usage
-----
::

    python scripts/ci/validate_dec_ids.py [pr_body_file]
    python scripts/ci/validate_dec_ids.py --sweep-open-prs

If ``pr_body_file`` is omitted, reads PR body from stdin (the CI
workflow pipes ``${{ github.event.pull_request.body }}`` in).

The appropriateness check consumes three env vars exported by the CI
job: ``PR_FILES`` (newline-separated changed files, output of
``git diff --name-only origin/$BASE_REF...HEAD``), ``PR_TITLE`` and
``PR_AUTHOR``. When ``PR_FILES`` or ``PR_TITLE`` is unset (local /
stdin invocations, pre-existing test harnesses) the appropriateness
check SKIPS with an ℹ️ note and exit 0 — mirroring
``validate_bugfix_regression.py``'s missing-env contract.

``--sweep-open-prs`` runs the SAME existence + appropriateness checks
over every OPEN PR via ``gh pr list --json number,title,body,files,
author`` — the blocking pre-merge sweep for landing the gate (fix any
tripping PR bodies in-flight before merging the gate itself).

Exit codes
----------
0 — every DEC-ID cited in the PR body has a matching MIGRATION entry
    AND the appropriateness check passes or is skipped/exempt.
1 — one or more cited DEC-IDs have no MIGRATION entry, OR the PR
    requires a decision citation and carries none (in sweep mode: any
    open PR trips either check).
2 — script error (e.g., MIGRATION.md missing, ``gh`` failure).

Heuristic scope (honest envelope per Golden Rule #10)
------------------------------------------------------
Appropriateness is a TITLE-TYPE heuristic, never full enforcement: a
``docs:``-titled PR touching ``brutex/*.py`` BYPASSES the gate (the
same deliberate title-only trade-off the sibling bug-fix scanner made
after PR #120 — the bypass envelope is pinned as a regression test so
it cannot silently change). A non-conventional title (no parseable
type) is treated as NON-exempt — fail-closed. "Cited the RIGHT
decision" stays reviewer judgement (STANDARDS row 18 stays PARTIAL).
Free bonus: the unfilled PR-template stub ``DEC-...`` matches no
DEC-ID (needs ``\\d{3,}``), so a source-touching PR that never filled
the template now fails loudly instead of sliding through.

Self-test overrides
-------------------
``DEC_ID_VALIDATOR_ALLOW_MISSING=1`` reverts the EXISTENCE check to
warn-and-continue for local script-iteration. CI never sets this.
``DEC_ID_APPROPRIATENESS_ALLOW_MISSING=1`` reverts the APPROPRIATENESS
check to warn-and-continue (emergency reverts; requires ADR per the
house escape-hatch convention). CI never sets this.
``PR_AUTHOR_EXEMPT_PATTERN`` overrides the bot-author exemption regex
(default reuses ``zplus-pr-guard.sh``'s value).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# These gate scripts run BOTH as scripts (CI: python scripts/ci/<name>.py,
# where the script dir is sys.path[0]) AND as importlib-loaded MODULES
# (the sibling-contract tests load them via spec_from_file_location, where
# it is not). Inject the script directory so the shared _textscan helper
# resolves in both modes (DEC-TEXTSCAN-COMMON-001).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _textscan import strip_code_spans

# ONE tests-predicate source of truth (Golden Rule #11 COMMON): import the
# sibling scanner's predicate, never copy it.
from validate_bugfix_regression import touches_tests

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = ROOT / "docs" / "MIGRATION.md"

# Pattern: DEC-FOO-NNN or DEC-FOO-BAR-NNN, etc.
# Anchored to be a "real" token, not a substring inside a larger word.
DEC_ID_PATTERN = re.compile(r"\bDEC-[A-Z][A-Z0-9_-]*-\d{3,}\b")

# Bot authors exempt from the appropriateness check. The default value is
# reused verbatim from scripts/ci/zplus-pr-guard.sh and is env-overridable
# under the same PR_AUTHOR_EXEMPT_PATTERN name, so the two gates never drift.
DEFAULT_AUTHOR_EXEMPT_PATTERN = r"^(renovate\[bot\]|dependabot\[bot\])$"

# Conventional-commit title types that are exempt from the appropriateness
# check — non-architectural surfaces by convention. A title whose type does
# not parse at all (non-conventional) is deliberately NON-exempt (fail-closed).
EXEMPT_TITLE_TYPES = frozenset({"docs", "chore", "test", "style", "ci"})

# Generalized anchored conventional-commit title shape (the sibling's
# BUGFIX_TITLE_PATTERN restricted to fix/bugfix/hotfix; this parses ANY type).
TITLE_TYPE_PATTERN = re.compile(r"^(\w+)(\([^)]*\))?[:!]")

# A diff touching the ledger itself satisfies the appropriateness predicate
# (the PR is adding/amending its own decision entry).
LEDGER_DIFF_PATH = "docs/MIGRATION.md"


def extract_dec_ids(text: str) -> list[str]:
    """Return all unique DEC-IDs found in ``text``, preserving first-appearance order.

    Strips markdown code spans first so example IDs inside ``backticks`` are not
    treated as live citations (e.g., PR-body documentation that uses
    ``DEC-NONEXISTENT-999`` to illustrate the fail case).
    """
    stripped = strip_code_spans(text)
    seen: set[str] = set()
    out: list[str] = []
    for match in DEC_ID_PATTERN.findall(stripped):
        if match not in seen:
            seen.add(match)
            out.append(match)
    return out


def load_known_dec_ids(migration_text: str) -> set[str]:
    """Extract every DEC-ID that appears as a ledger entry header in MIGRATION.md."""
    # An entry header looks like: "NNN. **DEC-FOO-001 — description**" or the
    # suffix-disambiguated "NNNa. **DEC-FOO-001 ..." (DEC-LEDGER-RENUMBER-001).
    # We accept any DEC-ID that appears bolded right after an entry-number —
    # INCLUDING every member of a plus-joined multi-DEC header ("NNN. **DEC-A +
    # DEC-B + DEC-C — …", the #300/#1297/#1529/#1759 one-line/multi-DEC entry
    # shape), since such an entry IS each member's ledger record. Conservative:
    # only the LEADING plus-chain is admitted — a DEC-ID merely mentioned later
    # in a title ("… supersedes DEC-X-001 —") or in the body does not resolve.
    header_pattern = re.compile(
        r"^\d+[a-z]?\.\s*\*\*"
        r"(DEC-[A-Z][A-Z0-9_-]*-\d{3,}(?:\s*\+\s*DEC-[A-Z][A-Z0-9_-]*-\d{3,})*)",
        re.MULTILINE,
    )
    known: set[str] = set()
    for chain in header_pattern.findall(migration_text):
        known.update(DEC_ID_PATTERN.findall(chain))
    return known


def read_pr_body(args: list[str]) -> str:
    """Read PR body from a file path (if given) or stdin."""
    if len(args) >= 2:
        return Path(args[1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def title_type(title: str) -> str | None:
    """Parse the conventional-commit type from a PR title, or None.

    ``feat(ci): x`` → ``"feat"``; ``docs: y`` → ``"docs"``; a
    non-conventional title (``Update stuff``) → ``None`` (treated as
    NON-exempt by the appropriateness check — fail-closed).
    """
    match = TITLE_TYPE_PATTERN.match(title.strip())
    return match.group(1).lower() if match else None


def is_behavioral_surface_file(path: str) -> bool:
    """Return True if *path* is on the behavioral surface the gate watches.

    Behavioral surface = ``brutex/**/*.py``, ``rust/**/*.rs``,
    ``.github/workflows/**``. Test files (per the COMMON
    ``touches_tests`` predicate) are excluded — a tests-only diff never
    requires a decision citation.
    """
    if touches_tests([path]):
        return False
    if path.startswith("brutex/") and path.endswith(".py"):
        return True
    if path.startswith("rust/") and path.endswith(".rs"):
        return True
    return path.startswith(".github/workflows/")


def requires_decision_citation(
    changed_files: list[str],
    title: str,
    author: str,
    cited_resolving_ids: list[str],
) -> bool:
    """True iff the PR MUST cite a resolving DEC-ID and does not.

    Fail-closed ONLY when ALL hold: (a) the author is not an exempt bot;
    (b) at least one changed file is on the behavioral surface; (c) the
    title type is not in the exempt set; (d) the body cites ZERO
    ledger-resolving DEC-IDs AND the diff does not touch
    ``docs/MIGRATION.md``.
    """
    exempt_pattern = os.environ.get("PR_AUTHOR_EXEMPT_PATTERN", DEFAULT_AUTHOR_EXEMPT_PATTERN)
    if re.match(exempt_pattern, author):
        return False
    if not any(is_behavioral_surface_file(f) for f in changed_files):
        return False
    if title_type(title) in EXEMPT_TITLE_TYPES:
        return False
    if cited_resolving_ids:
        return False
    return LEDGER_DIFF_PATH not in changed_files


def _check_appropriateness(cited_ids: list[str], known_ids: set[str]) -> int:
    """Run the appropriateness check off the PR_FILES/PR_TITLE/PR_AUTHOR env triple."""
    title = os.environ.get("PR_TITLE", "").strip()
    files_blob = os.environ.get("PR_FILES", "").strip()
    author = os.environ.get("PR_AUTHOR", "").strip()

    if not title or not files_blob:
        # Local / stdin invocations and pre-existing harnesses don't export
        # the triple — skip, mirroring validate_bugfix_regression.py.
        print(
            "ℹ️  PR_TITLE/PR_FILES not set; skipping DEC-ID appropriateness "
            "check (reviewer judgement)",
            file=sys.stderr,
        )
        return 0

    changed_files = [f.strip() for f in files_blob.splitlines() if f.strip()]
    resolving = [dec_id for dec_id in cited_ids if dec_id in known_ids]

    if not requires_decision_citation(changed_files, title, author, resolving):
        print(
            f"✅ DEC-ID appropriateness: PR '{title[:60]}' needs no decision "
            "citation (exempt/tests-only) or already carries one"
        )
        return 0

    if os.environ.get("DEC_ID_APPROPRIATENESS_ALLOW_MISSING") == "1":
        print(
            f"⚠️  PR '{title[:60]}' touches behavioral-surface files with no "
            "resolving DEC-ID citation and no docs/MIGRATION.md touch "
            "[DEC_ID_APPROPRIATENESS_ALLOW_MISSING=1, not failing]",
            file=sys.stderr,
        )
        return 0

    surface = [f for f in changed_files if is_behavioral_surface_file(f)]
    print(
        f"❌ MERGE BLOCKED — PR '{title[:60]}' (type "
        f"'{title_type(title) or 'unparseable'}') touches behavioral-surface "
        f"files but cites NO ledger-resolving DEC-ID and does not touch "
        "docs/MIGRATION.md:",
        file=sys.stderr,
    )
    for path in surface:
        print(f"  - {path}", file=sys.stderr)
    print(
        "\nFix: either (a) add a MIGRATION.md entry for this change and cite "
        "its DEC-ID in the PR body, (b) cite the pre-existing DEC-ID this "
        "change executes, or (c) retitle to docs:/chore:/test:/style:/ci: if "
        "genuinely non-architectural. See STANDARDS_100PCT.md §1 row 18 "
        "(partial-automation via scripts/ci/validate_dec_ids.py).",
        file=sys.stderr,
    )
    return 1


def sweep_open_prs() -> int:
    """``--sweep-open-prs``: run both checks over every OPEN PR via ``gh``.

    The blocking pre-merge sweep for landing the appropriateness gate:
    any open PR whose body cites a non-resolving DEC-ID, or that trips
    the appropriateness predicate, is reported so it can be fixed
    in-flight BEFORE the gate merges. Exit 0 = clean, 1 = trips found,
    2 = ``gh`` unavailable/failed (never a silent pass).

    Ledger-view limitation (honest envelope): the sweep resolves cited
    DEC-IDs against the LOCAL checkout's docs/MIGRATION.md, which
    predates any entry an open PR adds for ITSELF. A PR whose diff
    touches docs/MIGRATION.md therefore gets its non-resolving cited
    IDs excused here — its own per-PR CI run (which sees the merged
    tree) is the authoritative existence check for those.
    """
    gh_bin = shutil.which("gh")
    if gh_bin is None:
        print("⚠️  gh CLI not found on PATH — cannot sweep open PRs", file=sys.stderr)
        return 2
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            [
                gh_bin,
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title,body,files,author",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"⚠️  could not run gh pr list: {exc}", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print(
            f"⚠️  gh pr list exited {proc.returncode}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 2
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"⚠️  gh pr list emitted invalid JSON: {exc}", file=sys.stderr)
        return 2

    known_ids = load_known_dec_ids(MIGRATION_FILE.read_text(encoding="utf-8"))
    trips: list[str] = []
    for pr in prs:
        number = pr.get("number")
        title = str(pr.get("title") or "")
        body = str(pr.get("body") or "")
        files = [str(f.get("path") or "") for f in (pr.get("files") or [])]
        author = str((pr.get("author") or {}).get("login") or "")
        cited = extract_dec_ids(body)
        missing = [dec_id for dec_id in cited if dec_id not in known_ids]
        resolving = [dec_id for dec_id in cited if dec_id in known_ids]
        if missing and LEDGER_DIFF_PATH not in files:
            trips.append(
                f"PR #{number} '{title[:60]}': non-resolving DEC-IDs: {', '.join(missing)}"
            )
        elif requires_decision_citation(files, title, author, resolving):
            trips.append(
                f"PR #{number} '{title[:60]}': behavioral-surface change with "
                "no resolving DEC-ID citation and no docs/MIGRATION.md touch"
            )
    if trips:
        print(
            "❌ open-PR sweep found DEC-ID gate trips (fix in-flight before merging the gate):",
            file=sys.stderr,
        )
        for trip in trips:
            print(f"  - {trip}", file=sys.stderr)
        return 1
    print(f"✅ open-PR sweep clean: {len(prs)} open PR(s) pass both DEC-ID checks")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on any gate trip, 2 on script error."""
    argv = argv if argv is not None else sys.argv

    if not MIGRATION_FILE.exists():
        print(f"⚠️  MIGRATION.md not found at {MIGRATION_FILE}", file=sys.stderr)
        return 2

    if len(argv) >= 2 and argv[1] == "--sweep-open-prs":
        return sweep_open_prs()

    try:
        pr_body = read_pr_body(argv)
    except OSError as exc:
        print(f"⚠️  could not read PR body: {exc}", file=sys.stderr)
        return 2

    cited_ids = extract_dec_ids(pr_body)
    known_ids = load_known_dec_ids(MIGRATION_FILE.read_text(encoding="utf-8"))

    # ── Check 1: EXISTENCE — every cited DEC-ID resolves in the ledger ──
    if not cited_ids:
        # No DEC-IDs cited — nothing to existence-check. Whether one is
        # REQUIRED is the appropriateness check's question below.
        print("ℹ️  no DEC-IDs cited in PR body — nothing to existence-check")
    else:
        missing = [dec_id for dec_id in cited_ids if dec_id not in known_ids]
        if not missing:
            cited_list = ", ".join(cited_ids)
            print(f"✅ all {len(cited_ids)} cited DEC-IDs found in MIGRATION.md: {cited_list}")
        elif os.environ.get("DEC_ID_VALIDATOR_ALLOW_MISSING") == "1":
            # Warn-and-continue for local script-iteration only.
            for dec_id in missing:
                print(
                    f"⚠️  cited DEC-ID has no MIGRATION.md entry: {dec_id} "
                    "[DEC_ID_VALIDATOR_ALLOW_MISSING=1, not failing]",
                    file=sys.stderr,
                )
        else:
            print(
                "❌ MERGE BLOCKED — the following DEC-IDs are cited in the PR body "
                "but have no matching ledger entry in docs/MIGRATION.md:",
                file=sys.stderr,
            )
            for dec_id in missing:
                print(f"  - {dec_id}", file=sys.stderr)
            print(
                "\nFix: either (a) add the missing entry to docs/MIGRATION.md "
                "in this PR, or (b) remove the bogus DEC-ID from the PR body. "
                "See STANDARDS_100PCT.md §1 row 18 (partial-automation via "
                "scripts/ci/validate_dec_ids.py).",
                file=sys.stderr,
            )
            return 1

    # ── Check 2: APPROPRIATENESS — a behavioral-surface PR must cite ──
    # a resolving DEC-ID or touch the ledger (DEC-TEST-APPARATUS-DEBTS-001(e)).
    return _check_appropriateness(cited_ids, known_ids)


if __name__ == "__main__":
    sys.exit(main())

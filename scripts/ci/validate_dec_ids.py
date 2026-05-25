#!/usr/bin/env python3
"""DEC-ID validator — partial automation of STANDARDS_100PCT.md §1 row 18.

Closes the meta-gap that "every locked decision has a MIGRATION.md
entry" was 👁️ MANUAL with no automation. While full enforcement
(every concept change has a DEC-ID) requires reviewer judgement,
this script catches the most common failure mode:

* PR body cites ``DEC-FOO-001`` but no entry exists in
  ``docs/MIGRATION.md`` for that ID.

Usage
-----
::

    python scripts/ci/validate_dec_ids.py [pr_body_file]

If ``pr_body_file`` is omitted, reads PR body from stdin (the CI
workflow pipes ``${{ github.event.pull_request.body }}`` in).

Exit codes
----------
0 — every DEC-ID cited in the PR body has a matching MIGRATION entry
    (or the PR body cites no DEC-IDs at all — doc-only / config PRs).
1 — one or more cited DEC-IDs have no MIGRATION entry.
2 — script error (e.g., MIGRATION.md missing).

Heuristic scope (honest envelope per Golden Rule #10)
------------------------------------------------------
This script does NOT enforce that every PR MUST cite a DEC-ID — many
legitimate PRs (typo fixes, dependency bumps) don't have a locked
decision. It only catches the failure where a DEC-ID is CITED but
DOESN'T EXIST in the ledger. The "must cite DEC-ID for architectural
change" judgement stays with the reviewer.

Self-test override
------------------
``DEC_ID_VALIDATOR_ALLOW_MISSING=1`` reverts to warn-and-continue
for local script-iteration. CI never sets this.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = ROOT / "docs" / "MIGRATION.md"

# Pattern: DEC-FOO-NNN or DEC-FOO-BAR-NNN, etc.
# Anchored to be a "real" token, not a substring inside a larger word.
DEC_ID_PATTERN = re.compile(r"\bDEC-[A-Z][A-Z0-9_-]*-\d{3,}\b")


def strip_code_spans(text: str) -> str:
    """Remove markdown code spans (single + triple backticks) so example DEC-IDs
    in PR-body documentation (``"e.g., DEC-NONEXISTENT-999"``) don't trip the
    validator. Reviewer judgement still applies to whether a NON-code-span
    DEC-ID is real.
    """
    # Triple-backtick fenced blocks: ```...``` (greedy across newlines)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # Single-backtick inline code spans: `...`
    return re.sub(r"`[^`]*`", " ", text)


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
    # An entry header looks like: "NNN. **DEC-FOO-001 — description**"
    # We accept any DEC-ID that appears bolded right after an entry-number.
    header_pattern = re.compile(r"^\d+\.\s*\*\*(DEC-[A-Z][A-Z0-9_-]*-\d{3,})", re.MULTILINE)
    return set(header_pattern.findall(migration_text))


def read_pr_body(args: list[str]) -> str:
    """Read PR body from a file path (if given) or stdin."""
    if len(args) >= 2:
        return Path(args[1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on missing DEC-IDs, 2 on script error."""
    argv = argv if argv is not None else sys.argv

    if not MIGRATION_FILE.exists():
        print(f"⚠️  MIGRATION.md not found at {MIGRATION_FILE}", file=sys.stderr)
        return 2

    try:
        pr_body = read_pr_body(argv)
    except OSError as exc:
        print(f"⚠️  could not read PR body: {exc}", file=sys.stderr)
        return 2

    cited_ids = extract_dec_ids(pr_body)
    if not cited_ids:
        # No DEC-IDs cited — this script doesn't enforce that a PR
        # MUST cite one. Reviewer judgement still applies.
        print("ℹ️  no DEC-IDs cited in PR body — no validation needed")
        return 0

    known_ids = load_known_dec_ids(MIGRATION_FILE.read_text(encoding="utf-8"))
    missing = [dec_id for dec_id in cited_ids if dec_id not in known_ids]

    if not missing:
        cited_list = ", ".join(cited_ids)
        print(f"✅ all {len(cited_ids)} cited DEC-IDs found in MIGRATION.md: {cited_list}")
        return 0

    # Fail-closed unless the local-iteration override is set.
    if os.environ.get("DEC_ID_VALIDATOR_ALLOW_MISSING") == "1":
        for dec_id in missing:
            print(
                f"⚠️  cited DEC-ID has no MIGRATION.md entry: {dec_id} "
                "[DEC_ID_VALIDATOR_ALLOW_MISSING=1, not failing]",
                file=sys.stderr,
            )
        return 0

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


if __name__ == "__main__":
    sys.exit(main())

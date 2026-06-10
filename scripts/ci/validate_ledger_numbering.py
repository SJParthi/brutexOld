#!/usr/bin/env python3
"""validate_ledger_numbering.py — fail CI on NEW duplicate MIGRATION.md entry numbers.

DEC-LEDGER-RENUMBER-001 (executes follow-up F-1 of DEC-WORKSPACE-VERIFY-002,
``docs/MIGRATION.md`` entry #609). Parallel sessions appending independently
produced entry-number collisions (30 collision groups / 63 headers at the
2026-06-10 reconcile — e.g. four decisions racing for #613 in one
afternoon). DEC-IDs
stay unique, so the ledger is unambiguous BY DEC-ID; this gate makes the
NUMBERS trustworthy again and keeps them that way:

1. Every entry header must match ``^(\\d+)([a-z]?)\\. \\*\\*(DEC-...)``.
2. A (number, suffix) pair may appear at most ONCE — the historical
   collisions are disambiguated with letter suffixes (``613a.`` / ``613b.``)
   so existing "entry #NNN" citations still land on a visible, small
   candidate set instead of silently meaning the wrong decision.
3. A bare number may not coexist with suffixed forms of the same number
   (``613.`` next to ``613a.`` would re-introduce ambiguity).

Deliberately NOT a rule: a DEC-ID heading multiple entries. That is the
house multi-slice/amend pattern (e.g. DEC-OPTIONS-ELIGIBLE-GUARD-001 heads
the foundation/wiring/UI slices as three entries) — only the NUMBERS must
be unique; the historical pasted-text duplicate (#214) was suffixed like
every other collision (214a/214b — both copies preserved).

Usage::

    python scripts/ci/validate_ledger_numbering.py [path-to-MIGRATION.md]

Exit codes: 0 = ledger numbering is sound; 1 = violation (details on
stdout); 2 = script error. Fail-closed, mirroring the sibling PR-body
guards. ``LEDGER_NUMBERING_ALLOW_DUPES=1`` downgrades to warn-and-continue
for emergency operator override (logged loudly; requires an ADR per
CLAUDE.md Golden Rule #12).
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "docs" / "MIGRATION.md"

#: An entry header: ``613. **DEC-FOO-001 ...`` or the disambiguated
#: ``613a. **DEC-FOO-001 ...`` (suffix = one lowercase letter).
HEADER_RE = re.compile(r"^(\d{1,5})([a-z]?)\. \*\*(DEC-[A-Z][A-Z0-9_-]*-\d{3,})")


def scan(text: str) -> tuple[list[str], list[tuple[int, str, str, int]]]:
    """Return (violations, entries) for the ledger *text*.

    Each entry tuple is ``(number, suffix, dec_id, line_no)``.
    """
    entries: list[tuple[int, str, str, int]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        m = HEADER_RE.match(line)
        if m is not None:
            entries.append((int(m.group(1)), m.group(2), m.group(3), line_no))

    violations: list[str] = []

    # Rule 2: (number, suffix) unique.
    pair_counts = Counter((n, s) for n, s, _id, _ln in entries)
    for (n, s), count in sorted(pair_counts.items()):
        if count > 1:
            holders = [
                f"{_id} (line {ln})" for num, suf, _id, ln in entries if (num, suf) == (n, s)
            ]
            violations.append(
                f"entry number {n}{s or ''} is claimed {count}x: " + "; ".join(holders)
            )

    # Rule 3: bare number must not coexist with suffixed forms.
    bare = {n for n, s, _id, _ln in entries if s == ""}
    suffixed = {n for n, s, _id, _ln in entries if s != ""}
    for n in sorted(bare & suffixed):
        violations.append(
            f"entry number {n} exists BOTH bare and suffixed — suffix the bare "
            f"entry (e.g. {n}x.) so the candidate set stays explicit"
        )

    # Deliberately no DEC-ID-uniqueness rule: multi-slice decisions head
    # several entries by design (the amend pattern) — see module docstring.

    return violations, entries


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see module docstring for the contract."""
    args = argv if argv is not None else sys.argv
    ledger = Path(args[1]) if len(args) > 1 else DEFAULT_LEDGER
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read ledger at {ledger}: {exc}")
        return 2

    violations, entries = scan(text)
    if not violations:
        print(
            f"ledger numbering OK: {len(entries)} entry headers, every (number,suffix) pair unique"
        )
        return 0

    print("LEDGER NUMBERING VIOLATIONS (DEC-LEDGER-RENUMBER-001 gate):")
    for v in violations:
        print(f"  - {v}")
    print(
        "\nFix: give your NEW entry the next free number (max existing + 1, "
        "re-checked immediately before push — parallel rule P-1); never edit "
        "an existing entry's number without its own reviewed PR."
    )
    if os.environ.get("LEDGER_NUMBERING_ALLOW_DUPES") == "1":
        print(
            "WARNING: LEDGER_NUMBERING_ALLOW_DUPES=1 override active — NOT failing (ADR required)."
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

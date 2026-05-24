#!/usr/bin/env python3
"""Spec ↔ test crosswalk verifier.

Walks `docs/track2/CALCULATOR_SPEC.md` (and any other spec listed
below) for sections that prescribe testable behavior, then asserts
the corresponding test file exists and contains a matching test
function.

Per Golden Rule #12 requirement #8 (functionality completeness):
every spec section with testable behavior must have ≥ 1 test.

Exit codes
----------
0 — all spec sections have matching tests
1 — one or more sections have no matching test (BLOCKS PR)
2 — script error / spec file unreadable
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# Map: spec_file → (test_dir_glob, required_section_patterns)
# Each required_section_pattern is (regex_in_spec, expected_test_substring).
CROSSWALK: dict[Path, tuple[str, list[tuple[re.Pattern[str], str]]]] = {
    ROOT / "docs/track2/CALCULATOR_SPEC.md": (
        "tests/unit/test_costs_*.py",
        [
            (
                re.compile(r"### 12\.1 The canonical NIFTY fixture"),
                "test_fixture_12_1_nifty_canonical",
            ),
            (re.compile(r"### 12\.2 Additional fixtures"), "test_fixture_12_2"),
            (re.compile(r"### 12\.3 Multi-lot scaling"), "test_fixture_12_3_multi_lot"),
            (re.compile(r"### 12\.7 Long vs Short"), "test_fixture_12_7_side_symmetry"),
            (
                re.compile(r"## 4\.2 Trade outcome state machine"),
                "test_trade_outcome_state_machine",
            ),
            (re.compile(r"## 4\.3 Dhan-specific gotcha"), "test_dhan_worthless_brokerage"),
        ],
    ),
}


def main() -> int:
    failures: list[str] = []

    for spec_path, (test_glob, sections) in CROSSWALK.items():
        if not spec_path.exists():
            print(f"⚠️  spec file missing: {spec_path}", file=sys.stderr)
            return 2

        spec_text = spec_path.read_text(encoding="utf-8")

        test_files = list(ROOT.glob(test_glob))
        test_text_all = "\n".join(p.read_text(encoding="utf-8") for p in test_files)

        for section_re, expected_test in sections:
            if not section_re.search(spec_text):
                # FAIL-CLOSED per DEC-CROSSWALK-001 (2026-05-24, MIGRATION.md
                # entry #206). Previously this was warn-and-continue, which
                # allowed silent crosswalk drift — if anyone renamed a spec
                # section, the matching regex stopped matching and CI just
                # printed a warning nobody read. Now the PR that renames the
                # spec section MUST also update this script in the same PR.
                # That's the discipline the operator's Golden Rule #12
                # "100% standards" decree requires.
                #
                # Override during local script-iteration: env var
                # `CROSSWALK_ALLOW_STALE=1` reverts to warn-and-continue.
                # CI never sets this var; only local devs editing the
                # crosswalk patterns themselves use it.
                if os.environ.get("CROSSWALK_ALLOW_STALE") == "1":
                    print(
                        f"⚠️  spec section not found (crosswalk stale?): {section_re.pattern} "
                        "[CROSSWALK_ALLOW_STALE=1, not failing]",
                        file=sys.stderr,
                    )
                    continue
                failures.append(
                    f"❌ Spec section '{section_re.pattern}' in {spec_path.name} "
                    f"NOT FOUND — crosswalk regex is stale. Either (a) restore "
                    f"the section in the spec, or (b) update the regex in "
                    f"`scripts/ci/spec_test_crosswalk.py` to match the new "
                    f"section heading in the SAME PR. See MIGRATION.md "
                    f"entry #206 (DEC-CROSSWALK-001) for rationale."
                )
                continue

            if expected_test not in test_text_all:
                failures.append(
                    f"❌ Spec section '{section_re.pattern}' in {spec_path.name} "
                    f"has NO matching test function '{expected_test}' "
                    f"in any {test_glob}"
                )

    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)} spec ↔ test crosswalk failure(s)", file=sys.stderr)
        return 1

    print("✅ Spec ↔ test crosswalk: all required tests present")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
                # Spec section was renamed or removed — log a warning
                # but don't fail (the crosswalk script needs updating).
                print(
                    f"⚠️  spec section not found (crosswalk stale?): {section_re.pattern}",
                    file=sys.stderr,
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

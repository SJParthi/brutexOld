"""Generate the frozen pull-outcome classification goldens (DEC-RUST-PULL-STATE-001).

Run ONCE from the repo root on the PRE-conversion tree (before any Rust seam
touches ``brutex/dashboard/pull_outcome.py``):

    .venv/bin/python tests/golden/generate_pull_outcome_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``pull_outcome_classification.json`` — the full module-level oracle:
  every enumerated ``classify_last_pull_outcome`` +
  ``classify_retained_pull_outcome`` case with its exact
  ``LastPullOutcome.to_dict()`` output (the outcome/tone vocabulary is a
  FRONTEND wire contract — headline wordings are pinned byte-for-byte).
* ``pull_outcome_kernel_cases.tsv`` — the Rust decision-kernel contract rows
  (dependency-free tab-separated lines the crate's own ``cargo test`` reads —
  the cross-language pin; verdict-consuming arms are ``@verdict`` /
  ``@verdict_prepull`` markers because the banner fold + colour authority
  stay Python).

The consuming test (``test_pull_outcome_golden.py``) recomputes the SAME
tables from the live code and asserts structural equality, so the Rust
conversion is proven a pure re-hosting at every phase.  Case enumeration
lives in the shared ``tests/golden/_pull_outcome_harness.py`` (imported by
BOTH sides — the ``_market_hours_guard_harness`` shape).

Never regenerate on a post-conversion tree unless the classifier itself
legitimately moved (a new outcome token / a reworded headline under its own
DEC): the pre-conversion capture IS the oracle (Golden Rule #1).  The refusal
is MECHANICAL (the DEC-PREOPEN-PREVDAY-COMMON-002 pattern): when a fixture
already exists, :func:`main` raises ``SystemExit`` instead of silently
overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_PULL_OUTCOME_GOLDEN=1`` is set (a deliberate, ledger-worthy
act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._pull_outcome_harness import (
    compute_classify_table,
    compute_kernel_rows,
    compute_retained_table,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODULE_FIXTURE = FIXTURES / "pull_outcome_classification.json"
KERNEL_FIXTURE = FIXTURES / "pull_outcome_kernel_cases.tsv"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-conversion capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_PULL_OUTCOME_GOLDEN"


def main() -> None:
    existing = [p for p in (MODULE_FIXTURE, KERNEL_FIXTURE) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the PRE-conversion "
            f"oracle ({names}); a post-conversion tree must never silently "
            f"regenerate them (Golden Rule #1). Set {REGEN_ENV}=1 to overwrite "
            "deliberately."
        )
    FIXTURES.mkdir(parents=True, exist_ok=True)
    classify_rows = compute_classify_table()
    payload = {
        "schema": 1,
        "classify": classify_rows,
        "retained": compute_retained_table(),
    }
    MODULE_FIXTURE.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = compute_kernel_rows()
    for row in rows:  # the TSV contract: no field may smuggle a separator
        for field in row.split("\t"):
            if "\n" in field or "\r" in field:
                raise SystemExit(f"REFUSED: kernel fixture field contains a newline: {field!r}")
    KERNEL_FIXTURE.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {MODULE_FIXTURE} ({len(classify_rows)} classify rows)")
    print(f"wrote {KERNEL_FIXTURE} ({len(rows)} kernel rows)")


if __name__ == "__main__":
    main()

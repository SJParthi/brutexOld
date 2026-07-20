"""Generate the frozen pull-census goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

Run ONCE from the repo root on the PRE-conversion tree (the lane tip BEFORE
any Rust seam touches ``brutex/dashboard/pull_census.py``):

    .venv/bin/python tests/golden/generate_pull_census_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``pull_census_verdicts.json`` — the canonical payload: the census fold
  over representative + degenerate corpora (both mismatch classes, scope
  filters, the 20-sample cap, unicode/astral sort-order pins), each case
  carrying its mechanical kernel-input projection.
* ``pull_census_verdicts.tsv`` — the serde-free line projection of the SAME
  cases for the cargo fixture test
  (``rust/brutex-pull-state/tests/pull_census_golden.rs`` — the crate adds
  NO serde dependency; strings travel as UTF-8 hex).  The Python golden test
  pins the TSV byte-equal to the JSON's projection so the two can never
  drift.

The consuming test (``test_pull_census_golden.py``) recomputes the SAME
table from the live code and asserts structural equality — so the Rust
conversion is proven a pure re-hosting (verdicts bit-identical) at every
phase.  The verdict math lives in the shared
``tests/golden/_pull_census_harness.py`` (imported by BOTH sides — never
generator-imports-test).

Never regenerate on a post-conversion tree: the pre-conversion capture IS
the oracle (Golden Rule #1).  The refusal is MECHANICAL (the
DEC-PREOPEN-PREVDAY-COMMON-002 pattern): when a fixture already exists,
:func:`main` raises ``SystemExit`` instead of silently overwriting, unless
the explicit escape hatch ``BRUTEX_REGEN_PULL_CENSUS_GOLDEN=1`` is set (a
deliberate, ledger-worthy act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._pull_census_harness import compute_payload, compute_tsv

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_census_verdicts.json"
TSV_FIXTURE = FIXTURES / "pull_census_verdicts.tsv"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-conversion capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_PULL_CENSUS_GOLDEN"


def main() -> None:
    existing = [p for p in (JSON_FIXTURE, TSV_FIXTURE) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the PRE-conversion "
            f"oracle ({names}); a post-conversion tree must never silently "
            f"regenerate them (Golden Rule #1). Set {REGEN_ENV}=1 to overwrite "
            "deliberately."
        )
    FIXTURES.mkdir(parents=True, exist_ok=True)
    payload = compute_payload()
    JSON_FIXTURE.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    TSV_FIXTURE.write_text(compute_tsv(payload), encoding="utf-8")
    print(f"wrote {JSON_FIXTURE}")
    print(f"wrote {TSV_FIXTURE}")


if __name__ == "__main__":
    main()

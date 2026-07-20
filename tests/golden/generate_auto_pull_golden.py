"""Generate the frozen auto-pull predicate goldens (`DEC-RUST-PULL-STATE-001`, Wave 3G).

Run ONCE from the repo root on the PRE-conversion tree (the lane tip BEFORE
any Rust seam touches ``brutex/dashboard/auto_pull.py``):

    .venv/bin/python tests/golden/generate_auto_pull_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``auto_pull_reload_verdicts.json`` — the canonical payload: the
  ``should_reload_instrument_master`` predicate over count classes × the
  exhaustive flag lattice (out-of-i64 counts marked ``kernel_row=False`` —
  the documented Python arm).
* ``auto_pull_reload_verdicts.tsv`` — the serde-free line projection of the
  SAME kernel rows for the cargo fixture test
  (``rust/brutex-pull-state/tests/auto_pull_golden.rs``).

The consuming test (``test_auto_pull_golden.py``) recomputes the SAME table
from the live code and asserts structural equality.  The verdict math lives
in the shared ``tests/golden/_auto_pull_harness.py`` (imported by BOTH
sides — never generator-imports-test).

Never regenerate on a post-conversion tree: the pre-conversion capture IS
the oracle (Golden Rule #1).  The refusal is MECHANICAL: when a fixture
already exists, :func:`main` raises ``SystemExit`` unless the explicit
escape hatch ``BRUTEX_REGEN_AUTO_PULL_GOLDEN=1`` is set (a deliberate,
ledger-worthy act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._auto_pull_harness import compute_payload, compute_tsv

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "auto_pull_reload_verdicts.json"
TSV_FIXTURE = FIXTURES / "auto_pull_reload_verdicts.tsv"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-conversion capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_AUTO_PULL_GOLDEN"


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

"""Generate the frozen pull-scope note-surgery goldens (DEC-RUST-PULL-STATE-001).

Run ONCE from the repo root on the PRE-conversion tree (before any Rust seam
touches ``brutex/dashboard/pull_scope.py``):

    .venv/bin/python tests/golden/generate_pull_scope_golden.py

It freezes two renderings of ONE oracle table under
``tests/golden/fixtures/``:

* ``pull_scope_note_surgery.json`` — the Python-side oracle table
  (scope-token verdicts + market-hours rewrites + both note surgeries over
  REAL ``pipeline_pull`` producer notes, WARN arms included).
* ``pull_scope_note_surgery_rust.tsv`` — the flat escaped-TSV twin the
  dependency-free ``cargo test`` consumes via ``include_str!`` (the crate has
  no JSON parser; Cargo.toml deps are hub-frozen). The consuming Python test
  pins it byte-equal to the re-rendered JSON table — ONE oracle, two files.

The consuming test (``test_pull_scope_golden.py``) recomputes the SAME table
from the live code and asserts structural equality, so the Rust conversion is
proven a pure re-hosting (surgery bytes identical) at every phase. The table
math lives in the shared ``tests/golden/_pull_scope_harness.py`` (imported by
BOTH sides — never generator-imports-test).

Never regenerate on a post-conversion tree unless the ``pipeline_pull``
wording itself legitimately moved (a conscious anchor re-pin): the
pre-conversion capture IS the oracle (Golden Rule #1). The refusal is
MECHANICAL (the ``generate_market_hours_guard_golden`` pattern): when a
fixture already exists, :func:`main` raises ``SystemExit`` instead of
silently overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_PULL_SCOPE_GOLDEN=1`` is set (a deliberate, ledger-worthy act,
never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._pull_scope_harness import (
    compute_note_surgery_table,
    render_rust_fixture,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_scope_note_surgery.json"
TSV_FIXTURE = FIXTURES / "pull_scope_note_surgery_rust.tsv"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-conversion capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_PULL_SCOPE_GOLDEN"


def _dump_json(path: Path, payload: object) -> None:
    """Deterministic, compact, sorted-keys JSON (byte-stable regeneration)."""
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


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
    table = compute_note_surgery_table()
    _dump_json(JSON_FIXTURE, table)
    TSV_FIXTURE.write_text(render_rust_fixture(table), encoding="utf-8")
    print(f"wrote {JSON_FIXTURE}")
    print(f"wrote {TSV_FIXTURE}")


if __name__ == "__main__":
    main()

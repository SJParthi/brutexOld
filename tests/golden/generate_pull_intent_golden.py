"""Generate the frozen pull-intent verdict goldens (DEC-RUST-PULL-STATE-001, Wave 2E).

Run ONCE from the repo root on the PRE-conversion tree (before any Rust seam
touches ``brutex/dashboard/pull_intent.py``):

    .venv/bin/python tests/golden/generate_pull_intent_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``pull_intent_verdicts.json`` — the full module-level oracle: every
  enumerated sidecar-bytes corruption/valid shape driven through the REAL
  :class:`~brutex.dashboard.pull_intent.PullIntentStore` pipeline (verdict +
  the corrupt-delete effect + the mechanical kernel-input classification),
  the ``run_auto_resume`` arm lattice, the refused-start restore lattice,
  and the resume-env token law.
* ``pull_intent_kernel_cases.tsv`` — the Rust decision-kernel contract rows
  (dependency-free tab-separated lines the crate's own ``cargo test`` reads —
  the cross-language pin; rows whose integers do not fit the FFI stay on the
  documented Python arm and are excluded).

The consuming test (``test_pull_intent_golden.py``) recomputes the SAME
tables from the live code and asserts structural equality, so the Rust
conversion is proven a pure re-hosting at every phase.  Case enumeration
lives in the shared ``tests/golden/_pull_intent_harness.py`` (imported by
BOTH sides — the ``_pull_outcome_harness`` shape).

Never regenerate on a post-conversion tree unless the intent law itself
legitimately moved (a new field / a changed verdict under its own DEC): the
pre-conversion capture IS the oracle (Golden Rule #1).  The refusal is
MECHANICAL (the DEC-PREOPEN-PREVDAY-COMMON-002 pattern): when a fixture
already exists, :func:`main` raises ``SystemExit`` instead of silently
overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_PULL_INTENT_GOLDEN=1`` is set (a deliberate, ledger-worthy
act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._pull_intent_harness import (
    assert_no_env_override,
    compute_all_tables,
    compute_kernel_tsv,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
_JSON_PATH = _FIXTURE_DIR / "pull_intent_verdicts.json"
_TSV_PATH = _FIXTURE_DIR / "pull_intent_kernel_cases.tsv"
_REGEN_ENV = "BRUTEX_REGEN_PULL_INTENT_GOLDEN"


def main() -> None:
    """Freeze both fixtures (refusing to clobber without the escape hatch)."""
    assert_no_env_override()
    if os.environ.get(_REGEN_ENV, "") != "1":
        for path in (_JSON_PATH, _TSV_PATH):
            if path.exists():
                raise SystemExit(
                    f"{path} already exists — the pre-conversion capture IS the oracle "
                    f"(Golden Rule #1). Set {_REGEN_ENV}=1 to deliberately regenerate."
                )
    with tempfile.TemporaryDirectory(prefix="pull-intent-golden-") as tmp:
        tables = compute_all_tables(Path(tmp))
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(
        json.dumps(tables, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _TSV_PATH.write_text(
        compute_kernel_tsv(tables["parse"], tables["resume"], tables["restore"], tables["env"]),
        encoding="utf-8",
    )
    print(f"wrote {_JSON_PATH} ({len(tables['parse'])} parse cases)")
    print(f"wrote {_TSV_PATH}")


if __name__ == "__main__":
    main()

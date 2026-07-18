"""Generate the frozen rate-bridge read-verdict goldens (DEC-RUST-PULL-STATE-001, Wave 3H).

Run ONCE from the repo root on the PRE-conversion tree (before any Rust seam
touches ``brutex/data/rate_bridge.py``):

    .venv/bin/python tests/golden/generate_rate_bridge_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``rate_bridge_verdicts.json`` — the full module-level oracle: every
  enumerated bridge-file byte shape driven through the REAL
  :func:`brutex.data.rate_bridge.read_rate_snapshot` (outcome + the
  mechanical kernel-input classification).
* ``rate_bridge_kernel_cases.tsv`` — the Rust decision-kernel contract rows
  (dependency-free tab-separated lines the crate's own ``cargo test`` reads —
  the cross-language pin; corrupt-bytes + Python-arm rows are excluded by
  contract).

The consuming test (``test_rate_bridge_golden.py``) recomputes the SAME
tables from the live code and asserts structural equality, so the Rust
conversion is proven a pure re-hosting at every phase.  Case enumeration
lives in the shared ``tests/golden/_rate_bridge_harness.py``.

Never regenerate on a post-conversion tree unless the bridge law itself
legitimately moved (a schema bump / a new field under its own DEC): the
pre-conversion capture IS the oracle (Golden Rule #1).  The refusal is
MECHANICAL (the DEC-PREOPEN-PREVDAY-COMMON-002 pattern): when a fixture
already exists, :func:`main` raises ``SystemExit`` instead of silently
overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_RATE_BRIDGE_GOLDEN=1`` is set (a deliberate, ledger-worthy
act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._rate_bridge_harness import compute_payload, compute_tsv

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
_JSON_PATH = _FIXTURE_DIR / "rate_bridge_verdicts.json"
_TSV_PATH = _FIXTURE_DIR / "rate_bridge_kernel_cases.tsv"
_REGEN_ENV = "BRUTEX_REGEN_RATE_BRIDGE_GOLDEN"


def main() -> None:
    """Freeze both fixtures (refusing to clobber without the escape hatch)."""
    regen = os.environ.get(_REGEN_ENV, "").strip() == "1"
    for path in (_JSON_PATH, _TSV_PATH):
        if path.exists() and not regen:
            raise SystemExit(
                f"REFUSING to overwrite the frozen golden {path} — the "
                "pre-conversion capture is the oracle (Golden Rule #1). Set "
                f"{_REGEN_ENV}=1 only for a deliberate, ledgered regeneration."
            )
    payload = compute_payload()
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _TSV_PATH.write_text(compute_tsv(payload), encoding="utf-8")
    print(f"wrote {_JSON_PATH} ({len(payload['rows'])} rows)")
    print(f"wrote {_TSV_PATH}")


if __name__ == "__main__":
    main()

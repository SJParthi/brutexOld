"""Generate the frozen market-hours guard goldens (DEC-RUST-PULL-STATE-001).

Run ONCE from the repo root on the PRE-conversion tree (origin/main 415e863,
before any Rust seam touches ``brutex/calendars/market_hours.py``):

    .venv/bin/python tests/golden/generate_market_hours_guard_golden.py

It freezes two fixtures under ``tests/golden/fixtures/``:

* ``market_hours_guard_verdicts.json`` — ``pull_allowed_now`` over the FULL
  covered NSE-calendar span × the boundary/representative probe times + the
  out-of-coverage fail-safe cases (the vendor-account SAFETY contract of
  DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692).
* ``auto_pull_gate_verdicts.json`` — the ``--auto`` gate's deferral verdicts
  (allowed + machine reason) + the Muhurat margined-window boundaries
  (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md #1779).

The consuming test (``test_market_hours_guard_golden.py``) recomputes the SAME
tables from the live code and asserts structural equality — so the Rust
conversion lane is proven a pure re-hosting (verdicts byte-identical) at every
phase. The verdict math lives in the shared
``tests/golden/_market_hours_guard_harness.py`` (imported by BOTH sides — the
``_preopen_prevday_harness`` shape, never generator-imports-test).

Never regenerate on a post-conversion tree unless the CALENDAR itself
legitimately moved (a new NSE holiday circular year, a new Muhurat timing
entry): the pre-conversion capture IS the oracle (Golden Rule #1). The
refusal is MECHANICAL (the DEC-PREOPEN-PREVDAY-COMMON-002 pattern): when a
fixture already exists, :func:`main` raises ``SystemExit`` instead of silently
overwriting, unless the explicit escape hatch
``BRUTEX_REGEN_MARKET_HOURS_GOLDEN=1`` is set (a deliberate, ledger-worthy
act, never an accident).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._market_hours_guard_harness import (
    compute_auto_gate_table,
    compute_guard_table,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GUARD_FIXTURE = FIXTURES / "market_hours_guard_verdicts.json"
AUTO_GATE_FIXTURE = FIXTURES / "auto_pull_gate_verdicts.json"

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-conversion capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_MARKET_HOURS_GOLDEN"


def _dump(path: Path, payload: object) -> None:
    """Deterministic, compact, sorted-keys JSON (byte-stable regeneration)."""
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    existing = [p for p in (GUARD_FIXTURE, AUTO_GATE_FIXTURE) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the PRE-conversion "
            f"oracle ({names}); a post-conversion tree must never silently "
            f"regenerate them (Golden Rule #1). Set {REGEN_ENV}=1 to overwrite "
            "deliberately."
        )
    FIXTURES.mkdir(parents=True, exist_ok=True)
    _dump(GUARD_FIXTURE, compute_guard_table())
    _dump(AUTO_GATE_FIXTURE, compute_auto_gate_table())
    print(f"wrote {GUARD_FIXTURE}")
    print(f"wrote {AUTO_GATE_FIXTURE}")


if __name__ == "__main__":
    main()

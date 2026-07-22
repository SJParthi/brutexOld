"""Generate the frozen session-integrity goldens (DEC-EXTENDED-SESSION-OUTAGE-001
+ DEC-SESSION-DENSITY-CENSUS-001).

Run ONCE from the repo root on the POST-fix tree (these goldens pin the NEW
sanctioned behavior — the 16:50 extended-day forced exit + the census verdict
shape — so later refactors can never drift them silently):

    python tests/golden/generate_session_integrity_golden.py

The refusal is MECHANICAL (the ``generate_preopen_prevday_golden`` pattern):
when a fixture already exists, :func:`main` raises ``SystemExit`` instead of
silently overwriting the frozen oracle, unless the explicit escape hatch
``BRUTEX_REGEN_SESSION_INTEGRITY_GOLDEN=1`` is set (a deliberate,
ledger-worthy act, never an accident — Golden Rule #1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._session_integrity_harness import (
    FIXTURES,
    GOLDEN_CENSUS,
    GOLDEN_TRADES,
    census_payload,
    extended_day_trades_payload,
    render,
)

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the frozen capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_SESSION_INTEGRITY_GOLDEN"


def main() -> None:
    existing = [p for p in (GOLDEN_TRADES, GOLDEN_CENSUS) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the frozen oracle "
            f"({names}); never silently regenerate them (Golden Rule #1). "
            f"Set {REGEN_ENV}=1 to overwrite deliberately."
        )
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path, payload in (
        (GOLDEN_TRADES, extended_day_trades_payload()),
        (GOLDEN_CENSUS, census_payload()),
    ):
        data = render(payload)
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()

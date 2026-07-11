"""Generate the frozen NSE-calendar verdict-table golden (DEC-CALENDAR-VERDICT-GOLDEN-001).

Run ONCE from the repo root:

    python tests/golden/generate_calendar_verdict_table_golden.py

Pure reads of ``brutex.calendars.nse`` (via the shared
``tests/golden/_calendar_verdict_harness`` builders — no value hand-authored):
one compact verdict record per date over the FULL covered span
2020-01-01..2026-12-31 (2,557 records, one per line for reviewable diffs), the
two out-of-coverage abstain-triage strips, and every scalar-vector family
(expected-bars ceiling law, forced-exit times/seconds for one regular day +
EVERY Muhurat + special-weekend session, the ``is_forced_exit_bar`` strict-``>``
boundary, the ``daily_derive_cutoff`` strict-``>`` at-close law,
``session_last_bar_complete``, the ``previous_trading_session`` observed-overlay
remove-only vectors, and the Muhurat-precedence law).

REGENERATION LAW (differs from the audit fixture's never-regenerate law, stated
honestly): this fixture is DATA-CARRYING and MUST be regenerable when a
legitimate calendar-data update lands (``HOLIDAYS_2027``, the pending 2026-11-08
Muhurat timings, a Jan-2026 circular resolution). A regeneration REQUIRES its
own ledgered calendar-data DEC in the same commit; the consuming test's
``_EXPECTED_SHA256`` constant forces any regen through a reviewed test edit, and
this generator REFUSES to overwrite the existing fixture unless
``BRUTEX_REGEN_CALENDAR_VERDICT_GOLDEN=1`` is set (the
``BRUTEX_REGEN_PREOPEN_GOLDEN`` precedent) — a deliberate act, never an
accident.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
)

from tests.golden._calendar_verdict_harness import (
    CALENDAR_FIXTURE,
    FIXTURES,
    SPAN_END,
    SPAN_START,
    STRIP_AFTER,
    STRIP_BEFORE,
    build_days,
    build_scalars,
    build_strips,
    render_fixture,
)

#: The explicit overwrite escape hatch — a regen additionally requires its own
#: ledgered calendar-data DEC in the same commit (see module docstring).
REGEN_ENV: Final[str] = "BRUTEX_REGEN_CALENDAR_VERDICT_GOLDEN"


def main() -> None:
    if CALENDAR_FIXTURE.exists() and os.environ.get(REGEN_ENV) != "1":
        raise SystemExit(
            "REFUSED: the calendar verdict-table fixture already exists "
            f"({CALENDAR_FIXTURE}); regenerating it is a calendar-DATA act that "
            "requires its own ledgered DEC in the same commit (Golden Rule #1). "
            f"Set {REGEN_ENV}=1 ONLY for that deliberate, ledgered act."
        )
    meta = {
        "coverage_first_year": HOLIDAY_COVERAGE_FIRST_YEAR,
        "coverage_last_year": HOLIDAY_COVERAGE_LAST_YEAR,
        "span": [SPAN_START.isoformat(), SPAN_END.isoformat()],
        "strips": [
            [STRIP_BEFORE[0].isoformat(), STRIP_BEFORE[1].isoformat()],
            [STRIP_AFTER[0].isoformat(), STRIP_AFTER[1].isoformat()],
        ],
        "generator": "tests/golden/generate_calendar_verdict_table_golden.py",
        "generated_note": (
            "Machine-readable frozen answer key over brutex.calendars.nse "
            "(DEC-CALENDAR-VERDICT-GOLDEN-001). Regeneration requires "
            "BRUTEX_REGEN_CALENDAR_VERDICT_GOLDEN=1 AND its own ledgered "
            "calendar-data DEC in the same commit."
        ),
    }
    payload = render_fixture(meta, build_days(), build_strips(), build_scalars())
    FIXTURES.mkdir(parents=True, exist_ok=True)
    CALENDAR_FIXTURE.write_bytes(payload)
    print(f"wrote {CALENDAR_FIXTURE} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()

"""Unit tests for :mod:`brutex.dashboard.auto_pull`.

Pure-Python — no FastAPI, no DuckDB, no network, no Groww SDK. Exercises
the credentials-gated decision seam for the on-run expiries/contracts
auto-pull (DEC-AUTO-UNIVERSE-001) across all four
(credentials × instruments) combinations at 100% line + branch coverage.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.auto_pull import (
    should_auto_pull_universe,
    should_reload_instrument_master,
)


@pytest.mark.parametrize(
    ("index_count", "stock_count", "legs_present", "expected"),
    [
        # Wholly empty lake (fresh) → reload.
        (0, 0, False, True),
        # Stale lake: stocks present but ZERO indices (pre-fix build) → reload.
        (0, 211, False, True),
        # Healthy LEAN lake: indices present, no legs → skip (no reload).
        (25, 211, False, False),
        # Indices present, no stocks (degenerate but valid), no legs → skip.
        (30, 0, False, False),
        # Non-lean lake: legs still present (pre-DEC-INSTRUMENT-UNIVERSE-FAST-001
        # build) → reload to purge them, even though indices+stocks look fine.
        (25, 211, True, True),
        # Legs present even on an otherwise-empty lake → reload.
        (0, 0, True, True),
    ],
)
def test_should_reload_instrument_master(
    index_count: int, stock_count: int, legs_present: bool, expected: bool
) -> None:
    """Reload on empty OR indices-missing OR non-lean (legs present); else skip."""
    assert (
        should_reload_instrument_master(
            index_count=index_count,
            stock_count=stock_count,
            legs_present=legs_present,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("credentials_present", "instrument_total", "expected"),
    [
        # Both preconditions hold → pull.
        (True, 1, True),
        (True, 12345, True),
        # Credentials present but the instrument master is empty → skip.
        (True, 0, False),
        # Instruments loaded but no credentials → skip.
        (False, 1, False),
        # Neither precondition holds → skip.
        (False, 0, False),
    ],
)
def test_should_auto_pull_universe(
    credentials_present: bool, instrument_total: int, expected: bool
) -> None:
    """The pull runs iff credentials are present AND instruments are loaded."""
    assert (
        should_auto_pull_universe(
            credentials_present=credentials_present,
            instrument_total=instrument_total,
        )
        is expected
    )

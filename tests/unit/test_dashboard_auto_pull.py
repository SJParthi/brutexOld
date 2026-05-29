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
    ("index_count", "stock_count", "expected"),
    [
        # Wholly empty lake (fresh) → reload.
        (0, 0, True),
        # Stale lake: stocks present but ZERO indices (pre-fix build) → reload.
        (0, 211, True),
        # Healthy lake: indices present → skip (no reload).
        (30, 211, False),
        # Indices present, no stocks (degenerate but valid) → skip.
        (30, 0, False),
    ],
)
def test_should_reload_instrument_master(
    index_count: int, stock_count: int, expected: bool
) -> None:
    """Reload on an empty OR stale (indices-missing) lake; skip when indices exist."""
    assert (
        should_reload_instrument_master(
            index_count=index_count,
            stock_count=stock_count,
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

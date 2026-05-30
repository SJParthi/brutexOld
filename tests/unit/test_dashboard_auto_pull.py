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
    ("index_count", "stock_count", "legs_present", "stale_economics", "expected"),
    [
        # Wholly empty lake (fresh) → reload.
        (0, 0, False, False, True),
        # Stale lake: stocks present but ZERO indices (pre-fix build) → reload.
        (0, 211, False, False, True),
        # Healthy LEAN + enriched lake: indices present, no legs, real
        # economics → skip (no reload).
        (25, 211, False, False, False),
        # Indices present, no stocks (degenerate but valid), no legs → skip.
        (30, 0, False, False, False),
        # Non-lean lake: legs still present (pre-DEC-INSTRUMENT-UNIVERSE-FAST-001
        # build) → reload to purge them, even though indices+stocks look fine.
        (25, 211, True, False, True),
        # Legs present even on an otherwise-empty lake → reload.
        (0, 0, True, False, True),
        # Stale economics: a healthy-looking lean lake whose stocks still carry
        # the master's placeholder lot_size=1 (pre-back-fill build) → reload to
        # enrich (DEC-STALE-ECONOMICS-HEAL-001).
        (25, 211, False, True, True),
        # Stale economics takes priority even with legs absent + counts fine.
        (30, 100, False, True, True),
    ],
)
def test_should_reload_instrument_master(
    index_count: int,
    stock_count: int,
    legs_present: bool,
    stale_economics: bool,
    expected: bool,
) -> None:
    """Reload on empty OR indices-missing OR non-lean OR stale-economics; else skip."""
    assert (
        should_reload_instrument_master(
            index_count=index_count,
            stock_count=stock_count,
            legs_present=legs_present,
            stale_economics=stale_economics,
        )
        is expected
    )


def test_should_reload_defaults_stale_economics_false() -> None:
    """``stale_economics`` defaults to False (back-compat with 3-arg callers)."""
    assert (
        should_reload_instrument_master(index_count=25, stock_count=211, legs_present=False)
        is False
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

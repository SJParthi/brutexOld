"""Unit tests for ``brutex.adapters.groww.spot_bounds`` (DEC-SPOT-INTERVAL-DECOUPLE-001).

The SPOT 1-minute pull is now fully DECOUPLED from the 1-day coverage
(MIGRATION.md #851): the coupled discovery driver (``discover_spot_minute_windows``),
the pure coverage→window mapper (``spot_coverage_to_minute_window``), and the
lake-read helper (``spot_daily_coverage``) were all deleted. The module now exposes
ONLY the :data:`~brutex.adapters.groww.spot_bounds.SpotWindows` type alias — the
opt-in per-symbol window map still threaded (always ``None`` on the SPOT path)
through the ingest spine + window ledger.
"""

from __future__ import annotations

from datetime import date

from brutex.adapters.groww import spot_bounds as mod
from brutex.adapters.groww.spot_bounds import SpotWindows


def test_module_exports_only_the_spot_windows_alias() -> None:
    """The decoupled module's public surface is exactly the ``SpotWindows`` alias."""
    assert set(mod.__all__) == {"SpotWindows"}


def test_spot_windows_alias_is_the_per_symbol_window_map_type() -> None:
    """``SpotWindows`` is ``dict[str, tuple[date, date]]`` — a usable, well-typed map."""
    windows: SpotWindows = {"NIFTY": (date(2020, 1, 1), date(2026, 6, 18))}
    assert windows["NIFTY"] == (date(2020, 1, 1), date(2026, 6, 18))


def test_discovery_helpers_are_removed() -> None:
    """The coupled discovery/coverage helpers no longer exist (decoupled)."""
    for removed in (
        "discover_spot_minute_windows",
        "spot_coverage_to_minute_window",
        "spot_daily_coverage",
    ):
        assert not hasattr(mod, removed)

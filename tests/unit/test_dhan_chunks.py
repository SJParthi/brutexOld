"""Tests for `brutex.adapters.dhan.chunks` — the Dhan constant-pinned wrappers.

Targets 100% line+branch on the two Dhan-specific wrappers (90d spot / 30d option).
The generic ``plan_windows`` + ``DateWindow`` moved to
:mod:`brutex.adapters.ingest_common` (PR-J1) and are covered by
``test_ingest_common.py``; here they are only used as the manual oracle the
wrappers must match.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.adapters.dhan.chunks import (
    plan_option_windows,
    plan_spot_intraday_windows,
)
from brutex.adapters.dhan.constants import (
    INTRADAY_WINDOW_DAYS,
    ROLLING_OPTION_WINDOW_DAYS,
)
from brutex.adapters.ingest_common import plan_windows


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 15, tzinfo=UTC)


class TestConstantPinnedWrappers:
    def test_spot_uses_90_day_window(self) -> None:
        assert INTRADAY_WINDOW_DAYS == 90
        windows = plan_spot_intraday_windows(_dt(2021, 1, 1), _dt(2021, 7, 1))
        manual = plan_windows(_dt(2021, 1, 1), _dt(2021, 7, 1), window_days=90)
        assert windows == manual

    def test_option_uses_30_day_window(self) -> None:
        assert ROLLING_OPTION_WINDOW_DAYS == 30
        windows = plan_option_windows(_dt(2021, 1, 1), _dt(2021, 4, 1))
        manual = plan_windows(_dt(2021, 1, 1), _dt(2021, 4, 1), window_days=30)
        assert windows == manual

    def test_option_window_is_narrower_than_spot(self) -> None:
        span = (_dt(2021, 1, 1), _dt(2021, 6, 1))
        assert len(plan_option_windows(*span)) > len(plan_spot_intraday_windows(*span))

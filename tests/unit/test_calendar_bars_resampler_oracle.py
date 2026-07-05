"""Cross-oracle drift net: calendar bar-count predictor vs the real resampler.

DEC-CALENDAR-BARS-CEIL-001: :func:`brutex.calendars.nse.expected_bars_per_day_for_tf`
predicts the per-session bar count the lake's resampler
(:func:`brutex.data.tf_resampler.resample_1m_to_tf`) actually produces —
``ceil(375 / tf)`` including the truncated final bucket (the prior FLOOR
under-counted every non-divisor of 375, e.g. 60m read 6 vs the 7 bars stored).

This suite pins the two against each other for ALL 21 canonical lake
timeframes over a synthetic gap-free 375-bar 1-minute session, so any future
drift between the calendar predictor and the resampler fails loudly here.

Fixture style mirrors ``tests/unit/test_tf_resampler_session_bounds.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from brutex.calendars.nse import SESSION_DURATION_MINUTES, expected_bars_per_day_for_tf
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES

if TYPE_CHECKING:
    import polars as pl  # type-only: real module so `pl.X` annotations resolve
else:
    pl = pytest.importorskip("polars")  # runtime: frame construction needs the [data] extra

#: 09:15 IST == 03:45 UTC — the fixed session anchor.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45)

_SCHEMA = {
    "ts_utc": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _make_full_session() -> pl.DataFrame:
    """A gap-free full 375-bar 1-minute session (09:15 → 15:29 IST opens)."""
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    for i in range(SESSION_DURATION_MINUTES):
        ts = _SESSION_OPEN_UTC + timedelta(minutes=i)
        rows.append((ts, float(i), float(i + 2), float(i - 1), float(i + 1), i))
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row")


class TestCalendarPredictorMatchesResamplerOracle:
    """expected_bars_per_day_for_tf == resample_1m_to_tf(...).height, all 21 TFs."""

    def test_session_constants_agree(self) -> None:
        # The calendar's session length and the TF vocabulary's daily TF are
        # the same 375 minutes — the shared denominator both oracles divide.
        assert SESSION_DURATION_MINUTES == DAILY_TF_MINUTES == 375

    @pytest.mark.parametrize("tf", ALL_TF_MINUTES)
    def test_predicted_bars_equal_resampled_bars(self, tf: int) -> None:
        df = _make_full_session()
        out = resample_1m_to_tf(df, tf)
        assert expected_bars_per_day_for_tf(tf) == out.height, (
            f"tf={tf}: calendar predicts {expected_bars_per_day_for_tf(tf)} bars, "
            f"resampler emits {out.height}"
        )

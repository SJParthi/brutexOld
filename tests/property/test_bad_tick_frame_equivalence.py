"""Frame ⇔ scalar bad-tick screen equivalence (DEC-READ-GUARD-PARITY-001,
re-derived for DEC-BADTICK-HEAD-SEED-001 + DEC-BADTICK-HILO-SCREEN-001).

The two-phase :func:`brutex.data.bad_tick.screen_frame_bad_ticks` (vectorized
candidate trigger → sequential :meth:`~brutex.data.bad_tick.BadTickScreen.
screen_mask` arm) must keep EXACTLY the rows the scalar decision plane keeps
when fed the same ``(IST session date, close, high, low)`` sequence — the
equivalence oracle for NON-boundary drift: a wrong comparison direction, a
wrong session key, a wrong head-median window, a wrong baseline seat, an
under-triggering phase-1 arm (the #1811 blindness class — an UNDER-trigger
silently serves the poisoned frame as the identity object), or a
sequential-arm divergence over arbitrary generated walks. The identity⇔no-skip
law rides the same property: the INPUT OBJECT comes back IFF the mask keeps
every row. HONEST kill domain (Golden Rule #10): random floats essentially
never produce an exact ``prev * 4.0`` sample, so EXACT-boundary trigger drift
(``>=`` → ``>`` / ``<=`` → ``<``) is NOT killed here — the deterministic
single-direction boundary killers live in the unit battery
(``test_exact_up_boundary_sole_candidate_dropped`` /
``test_exact_down_boundary_sole_candidate_dropped``,
tests/unit/test_data_bad_tick.py).

Random positive close walks across 1-3 IST sessions — with occasional
non-finite closes (kept-unjudged, ARM 3), poison high/low extremes (the hi/lo
arm) and head-sized sessions (the median vote + unconfirmable 2-bar shape) —
naturally mix every decision class; the frame result's ``(ts, close)`` row
list must equal the scalar-kept list bit-for-bit.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.bad_tick import BadTickScreen, screen_frame_bad_ticks
from hypothesis import given
from hypothesis import strategies as st

#: 09:15 IST (03:45 UTC) on a regular session day — the per-day bar anchor.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: Positive close values wide enough to hit >=4x jumps, boundary multiples and
#: quiet walks alike (floats only — the lake's close column dtype), plus a
#: low-weight non-finite pool (the robustness law: kept-unjudged closes).
_CLOSES = st.one_of(
    st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf]),
)

#: High = close × mult / low = close ÷ div: mostly realistic intraday ranges,
#: occasionally a ×20 poison extreme so the hi/lo arm genuinely fires.
_RANGE_MULT = st.one_of(
    st.floats(min_value=1.0, max_value=1.5, allow_nan=False, allow_infinity=False),
    st.floats(min_value=1.0, max_value=1.5, allow_nan=False, allow_infinity=False),
    st.just(20.0),
)

_BAR = st.tuples(_CLOSES, _RANGE_MULT, _RANGE_MULT)


@given(
    sessions=st.lists(
        st.lists(_BAR, min_size=1, max_size=30),
        min_size=1,
        max_size=3,
    )
)
def test_frame_screen_matches_scalar_screen(
    sessions: list[list[tuple[float, float, float]]],
) -> None:
    """screen_frame_bad_ticks(frame) rows == the scalar screen_mask keeps."""
    stamps: list[datetime] = []
    dates = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for day, bars in enumerate(sessions):
        for minute, (close, h_mult, l_div) in enumerate(bars):
            ts = _SESSION_OPEN_UTC + timedelta(days=day, minutes=minute)
            stamps.append(ts)
            dates.append((ts + timedelta(hours=5, minutes=30)).date())
            closes.append(close)
            # A non-finite close propagates into its extremes (close*mult is
            # nan/inf) — exactly the poisoned-row shape ARM 3 exists for.
            highs.append(close * h_mult)
            lows.append(close / l_div)
    frame = pl.DataFrame(
        {
            "ts_utc": stamps,
            "high": highs,
            "low": lows,
            "close": closes,
        },
        schema={
            "ts_utc": pl.Datetime("us", "UTC"),
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
    )

    # The scalar oracle: the SAME (IST date, close, high, low) columns.
    keep, _events = BadTickScreen().screen_mask(dates, closes, highs=highs, lows=lows)
    expected = [(stamps[i], closes[i]) for i in range(len(stamps)) if keep[i]]

    result = screen_frame_bad_ticks(frame)
    got = list(
        zip(
            result.get_column("ts_utc").to_list(),
            result.get_column("close").to_list(),
            strict=True,
        )
    )

    def _key(pair: tuple[datetime, float]) -> tuple[datetime, float | str]:
        ts, close = pair
        return (ts, "nan" if math.isnan(close) else close)

    assert [_key(p) for p in got] == [_key(p) for p in expected]
    # The identity⇔no-skip law, BOTH directions: the input object comes back
    # IFF every row is kept (an under-triggering phase-1 would return the
    # identity object over a frame the sequential arm drops rows from).
    if all(keep):
        assert result is frame
    else:
        assert result is not frame
        assert result.height == len(expected)

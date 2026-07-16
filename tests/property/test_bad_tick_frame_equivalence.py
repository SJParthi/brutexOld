"""Frame ⇔ scalar bad-tick screen equivalence (DEC-READ-GUARD-PARITY-001).

The two-phase :func:`brutex.data.bad_tick.screen_frame_bad_ticks` (vectorized
candidate trigger → sequential :class:`~brutex.data.bad_tick.BadTickScreen`
arm) must keep EXACTLY the rows the scalar screen keeps when fed the same
``(IST session date, close)`` sequence bar-by-bar — the equivalence oracle
for NON-boundary drift: a wrong comparison direction, a wrong session key,
a wrong baseline, or a sequential-arm divergence over arbitrary generated
walks. HONEST kill domain (Golden Rule #10): random floats essentially never
produce an exact ``prev * 4.0`` sample, so EXACT-boundary trigger drift
(``>=`` → ``>`` / ``<=`` → ``<``) is NOT killed here — the deterministic
single-direction boundary killers live in the unit battery
(``test_exact_up_boundary_sole_candidate_dropped`` /
``test_exact_down_boundary_sole_candidate_dropped``,
tests/unit/test_data_bad_tick.py).

Random positive close walks across 1-3 IST sessions naturally mix clean runs,
genuine >=4x spikes, and spike successors (the baseline-hold class); the frame
result's ``(ts, close)`` row list must equal the scalar-kept list bit-for-bit.
"""

from __future__ import annotations

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
#: quiet walks alike (floats only — the lake's close column dtype).
_CLOSES = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)


@given(
    sessions=st.lists(
        st.lists(_CLOSES, min_size=1, max_size=30),
        min_size=1,
        max_size=3,
    )
)
def test_frame_screen_matches_scalar_screen(sessions: list[list[float]]) -> None:
    """screen_frame_bad_ticks(frame) rows == the scalar BadTickScreen keeps."""
    rows: list[tuple[datetime, float]] = []
    for day, closes in enumerate(sessions):
        for minute, close in enumerate(closes):
            rows.append((_SESSION_OPEN_UTC + timedelta(days=day, minutes=minute), close))
    frame = pl.DataFrame(
        {
            "ts_utc": [ts for ts, _ in rows],
            "close": [close for _, close in rows],
        },
        schema={"ts_utc": pl.Datetime("us", "UTC"), "close": pl.Float64},
    )

    # The scalar oracle: the SAME (IST date, close) sequence, bar by bar.
    screen = BadTickScreen()
    expected = [
        (ts, close)
        for ts, close in rows
        if screen.keep(ist_date=(ts + timedelta(hours=5, minutes=30)).date(), close=close)
    ]
    screen.finish()

    result = screen_frame_bad_ticks(frame)
    got = list(
        zip(
            result.get_column("ts_utc").to_list(),
            result.get_column("close").to_list(),
            strict=True,
        )
    )
    assert got == expected
    if len(expected) == len(rows):
        # Clean frame ⇒ the INPUT OBJECT comes back (the zero-copy contract).
        assert result is frame

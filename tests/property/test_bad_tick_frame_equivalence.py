"""Frame ⇔ scalar ⇔ kernel bad-tick screen equivalence (DEC-READ-GUARD-PARITY-001).

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

SLICE T24 PR-2 (DEC-DATA-RUST-INTEGRITY-DETECTORS-001): the gate widens to a
THREE-way — python-frame ≡ python-scalar ≡ the ``BRUTEX_FEED_RUST_FRAME``
kernel-routed frame arm (post-burn-in steady state, so the kernel decisions
genuinely SHIP), driven against the shared law-faithful fake kernel always
and the REAL built ``brutex_feed`` wheel when installed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("polars")

import brutex.data.bad_tick_rust as btr
import polars as pl
from brutex.data.bad_tick import BadTickScreen, screen_frame_bad_ticks
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes.feed_scan_crate import fake_feed_crate

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


def _rows_and_frame(
    sessions: list[list[float]],
) -> tuple[list[tuple[datetime, float]], pl.DataFrame]:
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
    return rows, frame


@pytest.mark.parametrize("kernel_source", ["fake", "real"])
@given(
    sessions=st.lists(
        st.lists(_CLOSES, min_size=1, max_size=30),
        min_size=1,
        max_size=3,
    )
)
def test_kernel_routed_frame_matches_scalar_screen(
    kernel_source: str, sessions: list[list[float]]
) -> None:
    """The 3-way gate: the kernel-routed frame arm ≡ the scalar oracle.

    SLICE T24 PR-2 — the routed arm runs at burn-in remaining=0 (kernel
    decisions genuinely ship, no oracle in the serving path) and must keep
    exactly the scalar-kept rows over the same hostile walks.
    """
    if kernel_source == "real":
        pytest.importorskip("brutex_feed")
    rows, frame = _rows_and_frame(sessions)

    screen = BadTickScreen()
    expected = [
        (ts, close)
        for ts, close in rows
        if screen.keep(ist_date=(ts + timedelta(hours=5, minutes=30)).date(), close=close)
    ]
    screen.finish()

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(btr.FEED_RUST_FRAME_ENV, "1")
        if kernel_source == "fake":
            mp.setitem(sys.modules, "brutex_feed", fake_feed_crate())
        btr.reset_bad_tick_rust_for_tests(remaining=0)
        try:
            result = screen_frame_bad_ticks(frame)
            latched = btr.frame_screen_rust_disabled()
        finally:
            btr.reset_bad_tick_rust_for_tests()
    got = list(
        zip(
            result.get_column("ts_utc").to_list(),
            result.get_column("close").to_list(),
            strict=True,
        )
    )
    assert got == expected
    assert latched is False  # the routed arm genuinely served (never latched)
    if len(expected) == len(rows):
        # Clean frame ⇒ phase 1 short-circuits on BOTH arms (input object).
        assert result is frame

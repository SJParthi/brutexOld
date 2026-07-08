"""Hypothesis invariants for the data-grounded daily cutoff (DEC-DAILY-CUTOFF-GROUNDED-001).

The DEFER-ONLY law, over the whole covered calendar span:

* ``grounded ∈ {clock, run_day}`` and ``grounded <= clock`` — the grounded arm
  can never promote past the clock verdict (zero look-ahead risk);
* clock parity whenever there is nothing to ground — a ``None`` last bar, a
  last bar predating the run day (run-day-only scoping: history is never
  re-opened), or a clock verdict that already excludes the run day;
* on a run day whose session the data proves COMPLETE, grounded == clock — the
  no-byte-change core of the debt item.

Dates are drawn INSIDE the verified holiday coverage (2020-2026) so
``get_session`` never fabricates (the out-of-coverage stance is the clock
arm's documented, separately-pinned envelope).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from brutex.calendars.nse import (
    IST,
    daily_derive_cutoff,
    daily_derive_cutoff_grounded,
    get_session,
    session_last_bar_complete,
)
from hypothesis import given
from hypothesis import strategies as st

_DATES = st.dates(min_value=date(2020, 1, 2), max_value=date(2026, 12, 30))
_TIMES = st.times()


def _ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


@given(d=_DATES, now_t=_TIMES, bar_offset_days=st.integers(-5, 1), bar_t=_TIMES)
def test_grounded_is_defer_only_and_bounded(
    d: date, now_t: time, bar_offset_days: int, bar_t: time
) -> None:
    now = _ist(d, now_t)
    last_bar = _ist(d + timedelta(days=bar_offset_days), bar_t)
    clock = daily_derive_cutoff(now)
    grounded = daily_derive_cutoff_grounded(now, last_bar_ist=last_bar)
    assert grounded in {clock, d}
    assert grounded <= clock


@given(d=_DATES, now_t=_TIMES)
def test_none_last_bar_is_always_clock_parity(d: date, now_t: time) -> None:
    now = _ist(d, now_t)
    assert daily_derive_cutoff_grounded(now, last_bar_ist=None) == daily_derive_cutoff(now)


@given(d=_DATES, now_t=_TIMES, days_back=st.integers(1, 10), bar_t=_TIMES)
def test_prior_day_last_bar_is_always_clock_parity(
    d: date, now_t: time, days_back: int, bar_t: time
) -> None:
    # Run-day-only scoping: yesterday (or older) never re-opens — no
    # permanent-deferral spiral.
    now = _ist(d, now_t)
    stale = _ist(d - timedelta(days=days_back), bar_t)
    assert daily_derive_cutoff_grounded(now, last_bar_ist=stale) == daily_derive_cutoff(now)


@given(d=_DATES, post_close_minutes=st.integers(1, 400))
def test_complete_run_day_session_is_always_clock_parity(d: date, post_close_minutes: int) -> None:
    # For every session shape (regular / Budget weekend / Muhurat evening):
    # the final stored bar (close - 1min, the half-open ingest filter's last
    # survivor) proves completeness ⇒ grounded == clock (today + 1).
    session = get_session(d)
    if session is None:
        return  # no session to complete — covered by the parity tests above
    close_dt = _ist(d, session.close_time_ist)
    now = close_dt + timedelta(minutes=post_close_minutes)
    if now.date() != d:
        return  # keep the run instant on the run day (near-midnight closes)
    last_bar = close_dt - timedelta(minutes=1)
    assert session_last_bar_complete(session, last_bar)
    clock = daily_derive_cutoff(now)
    assert clock == d + timedelta(days=1)
    assert daily_derive_cutoff_grounded(now, last_bar_ist=last_bar) == clock


@given(d=_DATES, post_close_minutes=st.integers(1, 400), missing_minutes=st.integers(1, 200))
def test_incomplete_run_day_session_always_defers(
    d: date, post_close_minutes: int, missing_minutes: int
) -> None:
    # A truncated run-day session (last bar strictly earlier than close - 1min)
    # is deferred to the run day — never finalized as a partial bar.
    session = get_session(d)
    if session is None:
        return
    close_dt = _ist(d, session.close_time_ist)
    now = close_dt + timedelta(minutes=post_close_minutes)
    if now.date() != d:
        return
    last_bar = close_dt - timedelta(minutes=1 + missing_minutes)
    if last_bar.date() != d:
        return  # truncation walked past midnight — not a run-day bar anymore
    assert not session_last_bar_complete(session, last_bar)
    assert daily_derive_cutoff_grounded(now, last_bar_ist=last_bar) == d

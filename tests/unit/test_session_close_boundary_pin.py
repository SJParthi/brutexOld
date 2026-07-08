"""The at-close (15:30:00.000000) cross-module drift-pin (DEC-SESSION-CLOSE-BOUNDARY-PIN-001).

The codebase carries TWO deliberately OPPOSITE at-close verdicts, frozen here
as a pinned pair (the #1267 conscious-divergence drift-pin pattern) so any
future flip of either side is a conscious, test-breaking act:

* **finalize side** — :func:`brutex.calendars.nse.daily_derive_cutoff` uses
  strict ``>``: at exactly 15:30:00 the run-day session reads STILL FORMING
  (cutoff = today, the run day's daily bar is NOT finalized). Rationale: the
  15:29 bar may not yet be pulled / session-filtered / written at that exact
  instant — finalizing would risk baking a partial daily bar; the deferral
  self-heals on the next run (latest-wins derived ``1day``). The ``>`` →
  ``>=`` "boundary tightening" follow-up of DEC-PREBUILD-SESSION-CLOSE-
  CUTOFF-001 is hereby CLOSED-AS-WONTFIX — it would shave only a one-minute
  self-healing window while making the verdict clock-race-dependent, and the
  data-grounded arm (DEC-DAILY-CUTOFF-GROUNDED-001) covers the real hazard.

* **request side** — :func:`brutex.adapters.ingest_common.clamp_window_end`
  uses ``>=``: at exactly 15:30:00 the session counts as CLOSED (the intraday
  pull bound may extend to ``today + 1`` end-exclusive). Rationale: an
  earliest-fetch of a finished session is safe — no bar at/after 15:30 can
  exist under the half-open ``[open, close)`` ingest session filter, and the
  partial-window detector + keep-first sink guard downstream.

Asking for MORE data one minute early is safe; DECLARING data final one
minute early is not — hence the pair, not a unification.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from brutex.adapters.ingest_common import clamp_window_end
from brutex.calendars.nse import IST, SESSION_CLOSE_IST, daily_derive_cutoff, get_session

#: A regular NSE trading Wednesday inside the verified holiday coverage.
_RUN_DAY = date(2026, 7, 1)


def _at_close() -> datetime:
    return datetime.combine(_RUN_DAY, SESSION_CLOSE_IST, tzinfo=IST)


def test_at_close_instant_the_two_verdicts_deliberately_diverge() -> None:
    """The pinned LAW at the SAME frozen instant (measure-zero in production —
    this pins the pair's polarity, not an observed bug): the finalize side
    still defers the run day while the request side already allows today+1."""
    instant = _at_close()
    session = get_session(_RUN_DAY)
    assert session is not None and session.close_time_ist == time(15, 30)

    # Finalize side: strict `>` — at-close = still forming ⇒ cutoff stays today.
    assert daily_derive_cutoff(instant) == _RUN_DAY

    # Request side: `>=` — at-close = closed ⇒ the intraday bound may reach
    # today + 1 (end-exclusive).
    assert clamp_window_end("1minute", _RUN_DAY + timedelta(days=30), now_ist=instant) == (
        _RUN_DAY + timedelta(days=1)
    )


def test_one_minute_past_close_the_two_verdicts_agree() -> None:
    """The divergence is EXACTLY the boundary instant: one minute later both
    sides read the session closed."""
    instant = _at_close() + timedelta(minutes=1)
    assert daily_derive_cutoff(instant) == _RUN_DAY + timedelta(days=1)
    assert clamp_window_end("1minute", _RUN_DAY + timedelta(days=30), now_ist=instant) == (
        _RUN_DAY + timedelta(days=1)
    )

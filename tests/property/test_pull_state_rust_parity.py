"""Kernel-vs-INLINE-LAW property for the market-hours guard
(DEC-RUST-PULL-STATE-001, flip phase).

The pre-flip live Python oracle is DELETED, so the differential partner is
an IN-TEST reimplementation of the window law (test code may reimplement
the law; production may not — the flip-contract test-restructure rule):
``allowed ⇔ not trading, or the IST wall-clock time outside
[08:45, 15:35)``. The wheel is REQUIRED — there is no skip: a missing
wheel fails these tests exactly as it fails the production guard
(fail-closed).

With the wheel present this proves:

* the hypothesis differential — the public ``pull_allowed_now`` (now
  kernel-backed) equals the inline law over ARBITRARY wall-clock instants
  (microsecond granularity, the four boundary microseconds always mixed in)
  × dates spanning pre-coverage, the full covered NSE calendar, and
  post-coverage, with the trading-day flag resolved by the ONE Python
  calendar AND forced over both polarities;
* the frozen-golden sweep — the kernel-backed guard reproduces EVERY
  verdict bit of the committed ``market_hours_guard_verdicts.json``
  fixture (fixtures BYTE-UNTOUCHED — the lane's byte-identity contract).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from brutex.calendars.market_hours import pull_allowed_now
from brutex.calendars.nse import IST, is_trading_day
from hypothesis import given
from hypothesis import strategies as st

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "market_hours_guard_verdicts.json"
)

#: The window boundaries + one-microsecond neighbours — always worth mixing
#: into the arbitrary-time draw (the exactness of the boundary law is the
#: whole point of the microsecond-of-day packing).
_BOUNDARY_TIMES = (
    time(8, 44, 59, 999999),
    time(8, 45, 0),
    time(8, 45, 0, 1),
    time(15, 34, 59, 999999),
    time(15, 35, 0),
    time(15, 35, 0, 1),
)

#: The window bounds, REIMPLEMENTED inline (never imported from production —
#: the whole point of the differential is an independent statement of the law).
_INLINE_BLOCK_START = time(8, 45)
_INLINE_BLOCK_END = time(15, 35)


def _inline_law(ist_dt: datetime, trading: bool) -> bool:
    """The INLINE reimplementation of the market-hours window law."""
    if not trading:
        return True
    return not (_INLINE_BLOCK_START <= ist_dt.time() < _INLINE_BLOCK_END)


@given(
    d=st.dates(min_value=date(2019, 1, 1), max_value=date(2030, 12, 31)),
    t=st.one_of(st.times(), st.sampled_from(_BOUNDARY_TIMES)),
)
def test_kernel_guard_equals_inline_law(d: date, t: time) -> None:
    """``pull_allowed_now`` ≡ the inline law, calendar-resolved flag included."""
    ist_dt = datetime.combine(d, t, tzinfo=IST)
    trading = is_trading_day(d)
    assert pull_allowed_now(ist_dt, trading) is _inline_law(ist_dt, trading)


@given(
    d=st.dates(min_value=date(2019, 1, 1), max_value=date(2030, 12, 31)),
    t=st.one_of(st.times(), st.sampled_from(_BOUNDARY_TIMES)),
    trading=st.booleans(),
)
def test_kernel_guard_equals_inline_law_forced_flag(d: date, t: time, trading: bool) -> None:
    """Both flag polarities, decoupled from the calendar (full input plane)."""
    ist_dt = datetime.combine(d, t, tzinfo=IST)
    assert pull_allowed_now(ist_dt, trading) is _inline_law(ist_dt, trading)


def test_kernel_guard_reproduces_the_frozen_golden() -> None:
    """The kernel-backed guard serves EVERY verdict bit of the frozen fixture.

    The lane's byte-identity contract: the frozen capture (pre-conversion
    Python over the whole covered NSE calendar + the out-of-coverage
    fail-safe rows) is reproduced by the RUST arm — 2557 days × 10 probes +
    the out-of-coverage rows, every bit compared. Fixture BYTE-UNTOUCHED.
    """
    frozen = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    probe_times = [time.fromisoformat(t) for t in frozen["probe_times"]]
    checked = 0
    for section in ("days", "out_of_coverage"):
        for day_iso, (trading_int, bits) in frozen[section].items():
            d = date.fromisoformat(day_iso)
            trading = bool(trading_int)
            for probe, expected_bit in zip(probe_times, bits, strict=True):
                ist_dt = datetime.combine(d, probe, tzinfo=IST)
                assert pull_allowed_now(ist_dt, trading) is (expected_bit == "1"), (day_iso, probe)
                checked += 1
    # Anti-vacuous: the sweep genuinely covered the whole fixture.
    assert checked == (2557 + 4) * len(probe_times)

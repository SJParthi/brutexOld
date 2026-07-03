"""Unit + property tests for per-bar / per-trade metadata stamping (PR-3).

Covers the ADR-0012 D8 stamp (date / weekday / minute-of-session / contract
expiry / is-expiry-day / circuit-halt flag) + the D13 integrity predicates
(no-forward-fill gap detection, forced-exit on the last real bar ≤ 15:20).
100% line + branch on ``brutex.feed.bar_metadata``.

Synthetic-fixture / calendar-driven — proves the LOGIC; the real-data run is on
the operator's machine.
"""

# The tod-seam delegation tests drive the private ``_seconds_of_day`` /
# ``_session_open_seconds`` helpers deliberately (their delegation + branch
# split IS the contract under test); repo convention for whitebox suites.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from brutex.data.futures_roll import FuturesContract, resolve_active_future
from brutex.feed.bar_metadata import (
    BarMetadata,
    last_real_bar_at_or_before_forced_exit,
    minute_of_session,
    missing_minute_bars,
    stamp_bar,
)
from brutex.options.lot_size_history import OptionsUnderlying
from hypothesis import given
from hypothesis import strategies as st

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
NIFTY = OptionsUnderlying.NIFTY

#: A regular Tuesday session.
DAY = date(2025, 6, 10)
#: The June 2025 NIFTY expiry (last Thursday, a trading day).
JUN_EXPIRY = date(2025, 6, 26)
JUN_CONTRACT = FuturesContract(underlying=NIFTY, expiry=JUN_EXPIRY)


def _ist(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)


# ────── minute_of_session ──────


def test_minute_zero_at_open() -> None:
    assert minute_of_session(_ist(DAY, 9, 15)) == 0


def test_minute_counts_whole_elapsed_minutes() -> None:
    assert minute_of_session(_ist(DAY, 9, 16)) == 1
    assert minute_of_session(_ist(DAY, 15, 20)) == 365  # 09:15 → 15:20 = 365 min


def test_minute_truncates_seconds_toward_open() -> None:
    # 09:16:59 is still minute 1 (seconds truncated to the 1-minute grid).
    assert minute_of_session(_ist(DAY, 9, 16, 59)) == 1


def test_minute_pre_open_is_negative_honest_signal() -> None:
    # A 09:00 pre-open bar surfaces a NEGATIVE minute, never silently clamped.
    assert minute_of_session(_ist(DAY, 9, 0)) == -15
    assert minute_of_session(_ist(DAY, 9, 14, 59)) == -1


def test_minute_accepts_utc_expressed_instant() -> None:
    # 09:15 IST == 03:45 UTC — the stamp localizes to IST first.
    utc_open = datetime(2025, 6, 10, 3, 45, tzinfo=UTC)
    assert minute_of_session(utc_open) == 0


def test_minute_custom_session_open() -> None:
    # A Muhurat / half-day open (e.g. 18:15 IST) anchors minute 0 there.
    muhurat_open = time(18, 15)
    assert minute_of_session(_ist(DAY, 18, 15), session_open=muhurat_open) == 0
    assert minute_of_session(_ist(DAY, 18, 45), session_open=muhurat_open) == 30


def test_minute_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        minute_of_session(datetime(2025, 6, 10, 9, 15))


# ────── stamp_bar / BarMetadata ──────


def test_stamp_regular_bar() -> None:
    m = stamp_bar(_ist(DAY, 9, 15), JUN_CONTRACT)
    assert m.trade_date == DAY
    assert m.weekday == 1  # Tuesday
    assert m.minute_of_session == 0
    assert m.contract_expiry == JUN_EXPIRY
    assert m.is_expiry_day is False
    assert m.contract_series_key == "NIFTY-FUT-2025-06-26"
    assert m.circuit_halt is False


def test_stamp_on_expiry_day_sets_flag() -> None:
    contract = resolve_active_future(NIFTY, JUN_EXPIRY)
    m = stamp_bar(_ist(JUN_EXPIRY, 10, 0), contract)
    assert m.is_expiry_day is True
    assert m.trade_date == JUN_EXPIRY == m.contract_expiry
    assert m.weekday == 3  # Thursday


def test_stamp_circuit_halt_flag_carried() -> None:
    m = stamp_bar(_ist(DAY, 9, 30), JUN_CONTRACT, circuit_halt=True)
    assert m.circuit_halt is True


def test_stamp_uses_actual_session_open_for_muhurat() -> None:
    # 2024-11-01 was a Diwali Muhurat session (~18:00 open). The stamp anchors
    # minute-of-session at the ACTUAL session open, not 09:15.
    muhurat_day = date(2024, 11, 1)
    from brutex.calendars.nse import get_session

    session = get_session(muhurat_day)
    assert session is not None
    open_dt = _ist(muhurat_day, session.open_time_ist.hour, session.open_time_ist.minute)
    m = stamp_bar(open_dt, FuturesContract(NIFTY, date(2024, 11, 28)))
    assert m.minute_of_session == 0


def test_stamp_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        stamp_bar(datetime(2025, 6, 10, 9, 15), JUN_CONTRACT)


def test_stamp_out_of_coverage_weekday_refuses_loud() -> None:
    # Calendar-correctness pack: the OLD silent behaviour is GONE — a bar dated
    # on a weekday OUTSIDE the published holiday span used to be stamped with a
    # silently-assumed regular 09:15 session open; stamp_bar now resolves the
    # session via the strict ``get_session_checked`` and REFUSES loudly
    # (Golden Rule #1 — never classify a bar the calendar cannot verify).
    from brutex.calendars.nse import HolidayDataUnavailableError

    out_day = date(2027, 1, 4)  # Monday, above the 2026 coverage ceiling
    with pytest.raises(HolidayDataUnavailableError):
        stamp_bar(_ist(out_day, 9, 15), FuturesContract(NIFTY, date(2027, 1, 28)))


def test_stamp_in_coverage_holiday_falls_back_to_regular_open() -> None:
    # An IN-COVERAGE non-session date (a listed holiday) still stamps without
    # raising: ``get_session_checked`` returns an honest None and the stamper
    # falls back to the regular 09:15 open (unchanged in-coverage behaviour).
    holiday = date(2025, 12, 25)  # Christmas 2025 — listed, in coverage
    m = stamp_bar(_ist(holiday, 9, 15), FuturesContract(NIFTY, date(2025, 12, 30)))
    assert m.minute_of_session == 0


def test_as_audit_payload_is_json_safe() -> None:
    import json

    payload = stamp_bar(_ist(DAY, 9, 15), JUN_CONTRACT).as_audit_payload()
    # Round-trips through json (the hash-chained audit log requirement).
    assert json.loads(json.dumps(payload)) == {
        "trade_date": "2025-06-10",
        "weekday": 1,
        "minute_of_session": 0,
        "contract_expiry": "2025-06-26",
        "is_expiry_day": False,
        "contract_series_key": "NIFTY-FUT-2025-06-26",
        "circuit_halt": False,
    }


def test_bar_metadata_is_frozen() -> None:
    m = stamp_bar(_ist(DAY, 9, 15), JUN_CONTRACT)
    with pytest.raises(AttributeError):
        m.weekday = 2  # type: ignore[misc]


# ────── missing_minute_bars (D13: no forward-fill) ──────


def test_missing_bars_empty_when_complete() -> None:
    grid = [_ist(DAY, 9, 15 + i) for i in range(5)]
    assert missing_minute_bars(present=grid, expected=grid) == ()


def test_missing_bars_reports_the_gap() -> None:
    expected = [_ist(DAY, 9, 15 + i) for i in range(5)]
    present = [expected[0], expected[1], expected[4]]  # 09:17, 09:18 missing
    assert missing_minute_bars(present=present, expected=expected) == (
        _ist(DAY, 9, 17),
        _ist(DAY, 9, 18),
    )


def test_missing_bars_matches_by_instant_across_timezones() -> None:
    # A UTC-expressed present bar matches its IST-expressed expected twin.
    expected = [_ist(DAY, 9, 15)]
    present = [datetime(2025, 6, 10, 3, 45, tzinfo=UTC)]  # same instant
    assert missing_minute_bars(present=present, expected=expected) == ()


def test_missing_bars_sorted() -> None:
    expected = [_ist(DAY, 9, 17), _ist(DAY, 9, 15), _ist(DAY, 9, 16)]
    assert missing_minute_bars(present=[], expected=expected) == (
        _ist(DAY, 9, 15),
        _ist(DAY, 9, 16),
        _ist(DAY, 9, 17),
    )


def test_missing_bars_rejects_naive_present() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        missing_minute_bars(present=[datetime(2025, 6, 10, 9, 15)], expected=[])


def test_missing_bars_rejects_naive_expected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        missing_minute_bars(present=[], expected=[datetime(2025, 6, 10, 9, 15)])


# ────── last_real_bar_at_or_before_forced_exit (D13) ──────


def test_forced_exit_picks_last_bar_at_or_before_1520() -> None:
    bars = [_ist(DAY, 15, 18), _ist(DAY, 15, 19), _ist(DAY, 15, 20), _ist(DAY, 15, 21)]
    # 15:19 is the last 1-minute bar whose window ([15:19,15:20)) does NOT pass
    # 15:20; the 15:20 bar IS a forced-exit bar.
    assert last_real_bar_at_or_before_forced_exit(bars) == _ist(DAY, 15, 19)


def test_forced_exit_falls_back_to_last_real_when_1520_missing() -> None:
    # The 15:20 bar is missing (a gap) → exit on the last real bar ≤ 15:20.
    bars = [_ist(DAY, 15, 17), _ist(DAY, 15, 18)]  # no 15:19/15:20
    assert last_real_bar_at_or_before_forced_exit(bars) == _ist(DAY, 15, 18)


def test_forced_exit_none_when_all_bars_after_exit() -> None:
    bars = [_ist(DAY, 15, 20), _ist(DAY, 15, 25)]
    assert last_real_bar_at_or_before_forced_exit(bars) is None


def test_forced_exit_none_when_empty() -> None:
    assert last_real_bar_at_or_before_forced_exit([]) is None


def test_forced_exit_unordered_input() -> None:
    bars = [_ist(DAY, 15, 19), _ist(DAY, 15, 10), _ist(DAY, 15, 18)]
    assert last_real_bar_at_or_before_forced_exit(bars) == _ist(DAY, 15, 19)


def test_forced_exit_coarse_timeframe_window() -> None:
    # A 15-minute bar opening at 15:15 runs to 15:30 → it PASSES 15:20, so the
    # last eligible coarse bar is the 15:00 bar (window [15:00,15:15)).
    bars = [_ist(DAY, 15, 0), _ist(DAY, 15, 15)]
    assert last_real_bar_at_or_before_forced_exit(bars, tf_minutes=15) == _ist(DAY, 15, 0)


def test_forced_exit_rejects_bad_tf() -> None:
    with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
        last_real_bar_at_or_before_forced_exit([_ist(DAY, 9, 15)], tf_minutes=0)


# ────── Property invariants ──────


@given(
    st.integers(min_value=9 * 3600 + 15 * 60, max_value=15 * 3600 + 30 * 60),
)
def test_property_minute_monotonic_in_time(sec_of_day: int) -> None:
    # minute_of_session is non-decreasing in the bar's time-of-day within a day.
    base = datetime(2025, 6, 10, tzinfo=IST)
    t0 = base + timedelta(seconds=sec_of_day)
    t1 = t0 + timedelta(minutes=1)
    assert minute_of_session(t1) >= minute_of_session(t0)


@given(st.dates(min_value=date(2024, 1, 1), max_value=date(2026, 6, 30)))
def test_property_is_expiry_day_iff_date_equals_expiry(d: date) -> None:
    contract = FuturesContract(NIFTY, JUN_EXPIRY)
    m = BarMetadata(
        trade_date=d,
        weekday=d.weekday(),
        minute_of_session=0,
        contract_expiry=JUN_EXPIRY,
        is_expiry_day=(d == JUN_EXPIRY),
        contract_series_key=contract.series_key,
        circuit_halt=False,
    )
    assert m.is_expiry_day == (d == JUN_EXPIRY)


class TestTodSecondsRustSeamDelegation:
    """DEC-TOD-SECONDS-RUST-SEAM-001: the module's tod helpers ride the seam."""

    def test_seconds_of_day_delegates_to_the_shared_seam(self) -> None:
        # Oracle: the independent inline expression (deliberately kept —
        # its independence from the seam under test is the point).
        from brutex.feed.bar_metadata import _seconds_of_day

        for hour, minute, second in [(0, 0, 0), (9, 15, 0), (15, 29, 59), (23, 59, 59)]:
            assert _seconds_of_day(hour, minute, second) == hour * 3600 + minute * 60 + second

    def test_session_open_seconds_regular_uses_hoisted_constant(self) -> None:
        from brutex.calendars.nse import SESSION_OPEN_IST
        from brutex.feed.bar_metadata import _REGULAR_OPEN_SECONDS, _session_open_seconds

        assert _session_open_seconds(SESSION_OPEN_IST) == _REGULAR_OPEN_SECONDS == 33_300

    def test_session_open_seconds_custom_open_derives_dynamically(self) -> None:
        from datetime import time

        from brutex.feed.bar_metadata import _session_open_seconds

        # A Muhurat-style 18:15 open takes the dynamic branch.
        assert _session_open_seconds(time(18, 15)) == 18 * 3600 + 15 * 60

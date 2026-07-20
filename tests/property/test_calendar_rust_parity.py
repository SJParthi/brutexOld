"""Hypothesis differential: the Rust calendar-verdict arm vs the Python oracle.

DEC-CALENDAR-VERDICT-RUST-001 (slice 9, ADR-0019): random dates over
1900..2100 (in- AND out-of-coverage — the Hinnant ``year_of_ordinal`` +
``weekday_of_ordinal`` helpers are differential-pinned here through the
public verdicts) × exchange selectors × observed-overlay sets, comparing the
FLAG-ON public functions (burn-in pre-exhausted so every verdict is
genuinely Rust-served) against the retained ``_py_*`` Python oracle bodies —
equal values AND equal exception types + messages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from brutex.calendars import nse
from hypothesis import given
from hypothesis import strategies as st

fno_math = pytest.importorskip("fno_math")
if not hasattr(fno_math, "CalendarVerdictEngine"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed fno_math wheel predates CalendarVerdictEngine (slice 9) — rebuild the wheel",
        allow_module_level=True,
    )

_DATES = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
_NEAR_COVERAGE_DATES = st.dates(min_value=date(2019, 12, 1), max_value=date(2027, 3, 1))
_SELS = st.sampled_from([None, "NSE", "BSE"])
_OBSERVED = st.frozensets(
    st.dates(min_value=date(2019, 12, 20), max_value=date(2027, 1, 20)), max_size=4
)


@pytest.fixture(autouse=True, scope="module")
def _rust_calendar_arm() -> Iterator[None]:
    """Arm the Rust arm module-wide with the burn-in PRE-EXHAUSTED.

    ``burn_in_remaining=0`` forces every verdict to be genuinely Rust-served
    (the burn-in path would otherwise serve the Python oracle's own value and
    make this differential vacuous).
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(nse.RUST_CALENDAR_ENV, "1")
        nse._reset_rust_calendar_for_tests(burn_in_remaining=0)  # pyright: ignore[reportPrivateUsage]
        assert nse._rust_calendar() is not None  # pyright: ignore[reportPrivateUsage]
        yield
    nse._reset_rust_calendar_for_tests()  # pyright: ignore[reportPrivateUsage]


def _outcome(call: Callable[[], object]) -> tuple[str, object]:
    """Project a call into a comparable (kind, payload) outcome — raises included."""
    try:
        return ("value", call())
    except nse.HolidayDataUnavailableError as exc:
        return ("HolidayDataUnavailableError", str(exc))


@given(d=_DATES, exchange=_SELS)
def test_is_trading_day_differential(d: date, exchange: str | None) -> None:
    """Unguarded trading-day verdicts equal over 1900..2100 × selectors."""
    assert nse.is_trading_day(d, exchange=exchange) == nse._py_is_trading_day(d, exchange)  # pyright: ignore[reportPrivateUsage]


@given(d=_DATES, exchange=_SELS)
def test_is_trading_day_checked_differential(d: date, exchange: str | None) -> None:
    """Checked triage equal INCLUDING the raise arm (type + message)."""
    rust = _outcome(lambda: nse.is_trading_day_checked(d, exchange=exchange))
    python = _outcome(lambda: nse._py_is_trading_day_checked(d, exchange))  # pyright: ignore[reportPrivateUsage]
    assert rust == python


@given(d=_DATES, exchange=_SELS)
def test_is_holiday_both_arms_differential(d: date, exchange: str | None) -> None:
    """``is_holiday`` + ``is_holiday_checked`` equal over the full domain."""
    holidays = (
        nse.NSE_HOLIDAYS
        if exchange == "NSE"
        else (nse.BSE_HOLIDAYS if exchange == "BSE" else nse.ALL_HOLIDAYS)
    )
    assert nse.is_holiday(d, exchange=exchange) == (d in holidays)
    rust = _outcome(lambda: nse.is_holiday_checked(d, exchange=exchange))
    python = _outcome(lambda: nse._py_is_holiday_checked(d, exchange))  # pyright: ignore[reportPrivateUsage]
    assert rust == python


@given(d=_DATES)
def test_weekday_and_membership_probes_differential(d: date) -> None:
    """``is_weekend`` (the weekday-of-ordinal pin) + the two data probes."""
    assert nse.is_weekend(d) == (d.weekday() >= 5)
    assert nse.has_muhurat_session(d) == (d in nse.MUHURAT_SESSIONS)
    assert nse.is_special_weekend_session(d) == (d in nse.SPECIAL_WEEKEND_TRADING_SESSIONS)


@given(d=_DATES, exchange=_SELS)
def test_get_session_both_arms_differential(d: date, exchange: str | None) -> None:
    """Sessions equal (incl. the out-of-coverage FABRICATION) on both variants."""
    assert nse.get_session(d, exchange=exchange) == nse._py_get_session(d, exchange)  # pyright: ignore[reportPrivateUsage]
    rust = _outcome(lambda: nse.get_session_checked(d, exchange=exchange))
    python = _outcome(lambda: nse._py_get_session_checked(d, exchange))  # pyright: ignore[reportPrivateUsage]
    assert rust == python


@given(d=_NEAR_COVERAGE_DATES, exchange=_SELS, observed=_OBSERVED)
def test_previous_trading_session_differential(
    d: date, exchange: str | None, observed: frozenset[date]
) -> None:
    """The back-walk (with the remove-only overlay) equals the oracle."""
    assert nse.previous_trading_session(
        d, exchange=exchange, observed=observed
    ) == nse._py_previous_trading_session(d, exchange, observed)  # pyright: ignore[reportPrivateUsage]


@given(start=_NEAR_COVERAGE_DATES, span=st.integers(min_value=0, max_value=21), exchange=_SELS)
def test_trading_days_between_both_arms_differential(
    start: date, span: int, exchange: str | None
) -> None:
    """Range walks equal on both variants (incl. the checked raise arm)."""
    end = start + timedelta(days=span)
    assert nse.trading_days_between(start, end, exchange=exchange) == nse._py_trading_days_between(  # pyright: ignore[reportPrivateUsage]
        start, end, exchange
    )
    rust = _outcome(lambda: nse.trading_days_between_checked(start, end, exchange=exchange))
    python = _outcome(lambda: nse._py_trading_days_between_checked(start, end, exchange))  # pyright: ignore[reportPrivateUsage]
    assert rust == python


@given(
    d=_NEAR_COVERAGE_DATES,
    seconds=st.integers(min_value=0, max_value=86_399),
    micro=st.integers(min_value=0, max_value=999_999),
)
def test_daily_derive_cutoff_differential(d: date, seconds: int, micro: int) -> None:
    """The strict-``>`` at-close law equals microsecond-for-microsecond."""
    now = datetime.combine(d, time(seconds // 3600, (seconds // 60) % 60, seconds % 60, micro))
    assert nse.daily_derive_cutoff(now) == nse._py_daily_derive_cutoff(now)  # pyright: ignore[reportPrivateUsage]


@given(tf=st.integers(min_value=1, max_value=500))
def test_expected_bars_differential(tf: int) -> None:
    """The ceiling law equals over every timeframe."""
    assert nse.expected_bars_per_day_for_tf(tf) == nse._py_expected_bars_per_day(tf)  # pyright: ignore[reportPrivateUsage]


@given(
    seconds=st.integers(min_value=0, max_value=86_399),
    tf=st.integers(min_value=1, max_value=400),
    boundary=st.one_of(st.none(), st.integers(min_value=0, max_value=86_399)),
)
def test_is_forced_exit_bar_differential(seconds: int, tf: int, boundary: int | None) -> None:
    """The strict-``>`` forced-exit law equals under default + custom bounds."""
    t = time(seconds // 3600, (seconds // 60) % 60, seconds % 60)
    resolved = nse._FORCED_EXIT_SECONDS if boundary is None else boundary  # pyright: ignore[reportPrivateUsage]
    assert nse.is_forced_exit_bar(t, tf, boundary) == nse._py_is_forced_exit_bar(t, tf, resolved)  # pyright: ignore[reportPrivateUsage]


@given(d=_NEAR_COVERAGE_DATES, last_minute=st.integers(min_value=0, max_value=1439))
def test_session_scalars_differential(d: date, last_minute: int) -> None:
    """Forced-exit seconds + same-date last-bar completeness equal the oracle."""
    session = nse._py_get_session(d, None)  # pyright: ignore[reportPrivateUsage]
    if session is None:
        return
    assert nse.forced_exit_seconds_for_session(session) == nse._py_forced_exit_seconds_for_session(
        session
    )  # pyright: ignore[reportPrivateUsage]
    last_bar = datetime.combine(d, time(last_minute // 60, last_minute % 60))
    assert nse.session_last_bar_complete(session, last_bar) == nse._py_session_last_bar_complete(  # pyright: ignore[reportPrivateUsage]
        session, last_bar
    )


def test_replay_left_the_kernel_engaged() -> None:
    """No differential example latched the autorevert — the aggregate proof."""
    assert nse._CAL_DISABLED_REASON[0] is None  # pyright: ignore[reportPrivateUsage]


def _sorted_ordinals(dates: frozenset[date]) -> list[int]:
    return sorted(d.toordinal() for d in dates)


@given(d=_DATES)
def test_year_and_weekday_pins_through_the_raw_engine(d: date) -> None:
    """The Hinnant helpers pinned against ``datetime.date`` over 1900..2100.

    ``is_trading_day_checked``'s raise arm is exactly the coverage-year test,
    and ``is_weekend`` is exactly the weekday test — probing the RAW engine
    keeps the pin independent of the seam's own dispatch.
    """
    kernel = nse._rust_calendar()  # pyright: ignore[reportPrivateUsage]
    assert kernel is not None
    engine: Any = kernel.engine
    assert bool(engine.is_weekend(d.toordinal())) == (d.weekday() >= 5)
    in_coverage = nse.HOLIDAY_COVERAGE_FIRST_YEAR <= d.year <= nse.HOLIDAY_COVERAGE_LAST_YEAR
    raises = int(engine.is_holiday_checked(d.toordinal(), 0)) == 2
    assert raises == (not in_coverage)

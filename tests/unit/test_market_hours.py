"""Tests for the NSE market-hours pull guard (DEC-GROWW-MARKET-HOURS-GUARD-001)."""

from __future__ import annotations

from datetime import datetime, time

import pytest
from brutex.calendars.market_hours import (
    BLOCK_END,
    BLOCK_START,
    MARKET_HOURS_GUARD_ENV,
    MarketHoursGuard,
    guard_active_default,
    market_hours_blocked_now,
    pull_allowed_default,
    pull_allowed_now,
    resolve_market_hours_guard_enabled,
)
from brutex.calendars.nse import IST

# A verified NSE trading WEEKDAY (Thu 2026-05-07 — inside holiday coverage, not a
# holiday) and a plain Saturday within coverage.
_TRADING_WEEKDAY = datetime(2026, 5, 7, tzinfo=IST)  # Thursday
_SATURDAY = datetime(2026, 5, 9, tzinfo=IST)  # Saturday


def _at(hh: int, mm: int, ss: int = 0, *, day: datetime = _TRADING_WEEKDAY) -> datetime:
    """A trading-weekday IST instant at the given wall-clock time."""
    return day.replace(hour=hh, minute=mm, second=ss)


# ── constants ────────────────────────────────────────────────────────────────


def test_window_boundary_constants() -> None:
    assert time(8, 45) == BLOCK_START
    assert time(15, 35) == BLOCK_END


# ── pull_allowed_now — the pure predicate ─────────────────────────────────────


@pytest.mark.parametrize(
    ("hh", "mm", "ss", "allowed"),
    [
        (8, 44, 59, True),  # one second before the window opens → allowed
        (8, 45, 0, False),  # exactly at open → blocked (inclusive)
        (10, 0, 0, False),  # mid-session → blocked
        (15, 34, 59, False),  # one second before close → blocked
        (15, 35, 0, True),  # exactly at close → allowed (exclusive)
        (0, 0, 0, True),  # midnight → allowed
        (1, 30, 0, True),  # the nightly 01:30 launch → allowed
        (23, 59, 59, True),  # late night → allowed
    ],
)
def test_pull_allowed_now_boundaries_on_trading_day(
    hh: int, mm: int, ss: int, allowed: bool
) -> None:
    dt = _at(hh, mm, ss)
    assert pull_allowed_now(dt, True) is allowed
    # market_hours_blocked_now is the exact negation.
    assert market_hours_blocked_now(dt, True) is (not allowed)


@pytest.mark.parametrize("hh", [1, 8, 10, 15, 23])
def test_non_trading_day_is_always_allowed(hh: int) -> None:
    dt = _at(hh, 30, day=_SATURDAY)
    assert pull_allowed_now(dt, False) is True
    assert market_hours_blocked_now(dt, False) is False


# ── pull_allowed_default — live-clock binding + fail-safe posture ─────────────


def test_pull_allowed_default_blocks_inside_window_on_trading_day() -> None:
    assert pull_allowed_default(now=_at(10, 0)) is False


def test_pull_allowed_default_allows_outside_window_on_trading_day() -> None:
    assert pull_allowed_default(now=_at(1, 30)) is True


def test_pull_allowed_default_allows_weekend() -> None:
    assert pull_allowed_default(now=_at(10, 0, day=_SATURDAY)) is True


def test_pull_allowed_default_out_of_coverage_weekday_blocks_in_window() -> None:
    """Fail-safe: an out-of-holiday-coverage weekday reads as a trading day → blocked."""
    # 2099-06-04 is a Thursday, far outside the holiday coverage span.
    future_weekday = datetime(2099, 6, 4, 10, 0, tzinfo=IST)
    assert future_weekday.weekday() == 3  # Thursday
    assert pull_allowed_default(now=future_weekday) is False


def test_pull_allowed_default_out_of_coverage_weekend_allowed() -> None:
    """A weekend is non-trading regardless of the (missing) holiday list → allowed."""
    future_saturday = datetime(2099, 6, 6, 10, 0, tzinfo=IST)
    assert future_saturday.weekday() == 5
    assert pull_allowed_default(now=future_saturday) is True


def test_pull_allowed_default_uses_live_clock_when_now_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: _at(10, 0))
    assert pull_allowed_default() is False


# ── resolve_market_hours_guard_enabled — the fail-loud env kill switch ─────────


@pytest.mark.parametrize("token", ["", "1", "on", "true", "yes", " ON ", "True"])
def test_env_enabled_tokens(token: str) -> None:
    assert resolve_market_hours_guard_enabled({MARKET_HOURS_GUARD_ENV: token}) is True


@pytest.mark.parametrize("token", ["0", "off", "false", "no", " OFF ", "No"])
def test_env_disabled_tokens(token: str) -> None:
    assert resolve_market_hours_guard_enabled({MARKET_HOURS_GUARD_ENV: token}) is False


def test_env_default_enabled_when_unset() -> None:
    assert resolve_market_hours_guard_enabled({}) is True


def test_env_junk_value_raises() -> None:
    with pytest.raises(ValueError, match="not a recognised switch value"):
        resolve_market_hours_guard_enabled({MARKET_HOURS_GUARD_ENV: "maybe"})


def test_env_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MARKET_HOURS_GUARD_ENV, raising=False)
    assert resolve_market_hours_guard_enabled() is True
    monkeypatch.setenv(MARKET_HOURS_GUARD_ENV, "off")
    assert resolve_market_hours_guard_enabled() is False


# ── guard_active_default ──────────────────────────────────────────────────────


def test_guard_active_default_true_when_enabled_and_blocked() -> None:
    assert guard_active_default(now=_at(10, 0), env={}) is True


def test_guard_active_default_false_when_enabled_but_allowed() -> None:
    assert guard_active_default(now=_at(1, 30), env={}) is False


def test_guard_active_default_false_when_disabled() -> None:
    assert guard_active_default(now=_at(10, 0), env={MARKET_HOURS_GUARD_ENV: "off"}) is False


# ── MarketHoursGuard object ───────────────────────────────────────────────────


def test_guard_object_blocked_inside_window() -> None:
    guard = MarketHoursGuard(enabled=True, clock=lambda: _at(10, 0))
    assert guard.blocked() is True


def test_guard_object_not_blocked_outside_window() -> None:
    guard = MarketHoursGuard(enabled=True, clock=lambda: _at(1, 30))
    assert guard.blocked() is False


def test_guard_object_disabled_never_blocks() -> None:
    guard = MarketHoursGuard(enabled=False, clock=lambda: _at(10, 0))
    assert guard.blocked() is False


def test_guard_object_advancing_clock_flips_at_open() -> None:
    """A clock that advances past 08:45 flips blocked False → True."""
    times = iter([_at(8, 44, 59), _at(8, 45, 0)])
    guard = MarketHoursGuard(enabled=True, clock=lambda: next(times))
    assert guard.blocked() is False
    assert guard.blocked() is True


def test_guard_from_env_resolves_enabled_and_clock() -> None:
    guard = MarketHoursGuard.from_env(clock=lambda: _at(10, 0), env={})
    assert guard.enabled is True
    assert guard.blocked() is True


def test_guard_from_env_disabled() -> None:
    guard = MarketHoursGuard.from_env(
        clock=lambda: _at(10, 0), env={MARKET_HOURS_GUARD_ENV: "false"}
    )
    assert guard.enabled is False
    assert guard.blocked() is False


def test_guard_from_env_default_clock_is_now_ist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: _at(10, 0))
    guard = MarketHoursGuard.from_env(env={})
    assert guard.blocked() is True

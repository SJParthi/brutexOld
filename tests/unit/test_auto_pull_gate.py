"""``brutex.calendars.auto_pull_gate`` — the ``pull-all-spot --auto`` gate.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779): the strict fail-loud
settle-time resolver (structurally ≥ the market-hours guard's 15:35 BLOCK_END),
the whole-day AUTO verdict (trading day → Muhurat → settle → belt), the pure
margined ``muhurat_session_active`` window, and the ``compose_market_closed``
mid-run belt over the UNTOUCHED guard. 100% line+branch standalone (the ci.yml
module gate).
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from brutex.calendars import auto_pull_gate
from brutex.calendars.auto_pull_gate import (
    DEFAULT_PULL_ALL_SETTLE_IST,
    MUHURAT_POST_MARGIN,
    MUHURAT_PRE_MARGIN,
    PULL_ALL_SETTLE_ENV,
    AutoGateVerdict,
    auto_pull_allowed,
    compose_market_closed,
    muhurat_session_active,
    resolve_settle_time,
)
from brutex.calendars.market_hours import BLOCK_END
from brutex.calendars.nse import MUHURAT_SESSIONS, is_trading_day
from structlog.testing import capture_logs

#: A plain NSE trading day (Tuesday, inside holiday coverage).
_TRADING_DAY = date(2024, 1, 2)


def _at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss)


# ── resolve_settle_time ──────────────────────────────────────────────────


def test_resolve_settle_default_17_30() -> None:
    assert resolve_settle_time({}) == time(17, 30)
    assert resolve_settle_time({PULL_ALL_SETTLE_ENV: ""}) == time(17, 30)
    assert resolve_settle_time({PULL_ALL_SETTLE_ENV: "   "}) == time(17, 30)
    assert time(17, 30) == DEFAULT_PULL_ALL_SETTLE_IST


def test_resolve_settle_reads_os_environ_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PULL_ALL_SETTLE_ENV, "18:00")
    assert resolve_settle_time() == time(18, 0)


@pytest.mark.parametrize(
    "junk", ["25:99", "1730", "x", "9:30", "17:30:00", "24:00", "17:60", "17-30"]
)
def test_settle_env_junk_raises(junk: str) -> None:
    """Fail-loud strict HH:MM — junk is never silently coerced."""
    with pytest.raises(ValueError, match=PULL_ALL_SETTLE_ENV):
        resolve_settle_time({PULL_ALL_SETTLE_ENV: junk})


def test_settle_before_block_end_refused_15_35_allowed() -> None:
    """The STRUCTURAL floor: < 15:35 refused naming the guard; 15:35 exact OK."""
    with pytest.raises(ValueError, match="BLOCK_END 15:35"):
        resolve_settle_time({PULL_ALL_SETTLE_ENV: "15:34"})
    with pytest.raises(ValueError, match="market-hours"):
        resolve_settle_time({PULL_ALL_SETTLE_ENV: "09:00"})
    assert resolve_settle_time({PULL_ALL_SETTLE_ENV: "15:35"}) == BLOCK_END


def test_settle_cli_style_mapping_override() -> None:
    """The CLI --settle-ist one-shot override rides the SAME mapping seam."""
    env = {PULL_ALL_SETTLE_ENV: "16:00"}
    assert resolve_settle_time(env) == time(16, 0)
    v = auto_pull_allowed(now=_at(_TRADING_DAY, 15, 45), env=env)
    assert (v.allowed, v.reason) == (False, "before_settle")
    assert "16:00" in v.detail
    assert auto_pull_allowed(now=_at(_TRADING_DAY, 16, 0), env=env).allowed is True


# ── auto_pull_allowed ────────────────────────────────────────────────────


def test_gate_junk_env_raises_out() -> None:
    """Junk settle env RAISES out of the gate — the caller fails loud pre-vendor."""
    with pytest.raises(ValueError, match=PULL_ALL_SETTLE_ENV):
        auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env={PULL_ALL_SETTLE_ENV: "nope"})


@pytest.mark.parametrize(("hh", "mm"), [(8, 45), (9, 15), (12, 0), (15, 0), (15, 34)])
def test_gate_blocked_every_market_hours_minute(hh: int, mm: int) -> None:
    """Every in-window minute defers — via before_settle (< the 17:30 default,
    which the ≥ 15:35 settle floor makes the STRUCTURALLY first-reached arm)."""
    v = auto_pull_allowed(now=_at(_TRADING_DAY, hh, mm), env={})
    assert v.allowed is False
    assert v.reason == "before_settle"


def test_gate_15_34_59_and_15_35_00_defer_before_settle() -> None:
    """B C1/C2 boundary: both sides of BLOCK_END still wait for the settle."""
    for now in (_at(_TRADING_DAY, 15, 34, 59), _at(_TRADING_DAY, 15, 35, 0)):
        v = auto_pull_allowed(now=now, env={})
        assert (v.allowed, v.reason) == (False, "before_settle")
        assert "UNMEASURED" in v.detail
        assert "POSTCLOSE_SETTLE_RUNBOOK.md" in v.detail
        assert PULL_ALL_SETTLE_ENV in v.detail


def test_gate_boundary_17_29_defers_17_30_allows() -> None:
    """C 9: the settle boundary is INCLUSIVE — 17:30 exactly is allowed."""
    before = auto_pull_allowed(now=_at(_TRADING_DAY, 17, 29), env={})
    assert (before.allowed, before.reason) == (False, "before_settle")
    at = auto_pull_allowed(now=_at(_TRADING_DAY, 17, 30), env={})
    assert at == AutoGateVerdict(allowed=True, reason="ok", detail="ok")


def test_gate_weekend_holiday_non_trading() -> None:
    """B C9: weekend + listed holiday both defer as non_trading_day."""
    sunday = date(2026, 7, 12)
    holiday = date(2026, 11, 10)  # Diwali Balipratipada (listed, Tuesday)
    for d in (sunday, holiday):
        v = auto_pull_allowed(now=_at(d, 18, 0), env={})
        assert v.allowed is False
        assert v.reason == "non_trading_day"
        assert v.detail == f"{d.isoformat()} is not an NSE trading day — nothing new to pull"


def test_gate_all_muhurat_dates_whole_day_deferred() -> None:
    """A/B C4: EVERY listed Muhurat date + the announced-but-timing-unnotified
    2026 Diwali dates defer for the WHOLE day (17:30 AND 19:00) — every listed
    Muhurat date is a holiday, so the non-trading arm already defers; the
    muhurat arm is the belt."""
    for d in (*MUHURAT_SESSIONS, date(2026, 11, 8), date(2026, 11, 10)):
        for hh in (17, 19):
            v = auto_pull_allowed(now=_at(d, hh, 0), env={})
            assert v.allowed is False, f"{d} @ {hh}:00 must defer"
            assert v.reason in {"non_trading_day", "muhurat_deferral"}


def test_gate_muhurat_belt_fires_even_if_trading_flag_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B C4 belt: a future TRADING-day Muhurat listing still defers (step 2)."""
    muhurat_day = date(2025, 10, 21)  # afternoon session 13:45-14:45
    assert is_trading_day(muhurat_day) is False  # today: a listed holiday
    monkeypatch.setattr(auto_pull_gate, "is_trading_day", lambda _d: True)
    v = auto_pull_allowed(now=_at(muhurat_day, 18, 0), env={})
    assert (v.allowed, v.reason) == (False, "muhurat_deferral")
    assert "evening-session settle behavior unverified" in v.detail


def test_gate_out_of_coverage_weekday_allows_post_settle_with_one_warn() -> None:
    """Ruling 10 / B C8: a ≥2027 weekday PROCEEDS post-settle with ONE WARN —
    the zero-touch product survives the calendar horizon; the untouched
    market-hours guard still blocks in-window."""
    monday_2027 = date(2027, 1, 4)
    with capture_logs() as logs:
        v = auto_pull_allowed(now=_at(monday_2027, 18, 0), env={})
    assert v == AutoGateVerdict(allowed=True, reason="ok", detail="ok")
    warns = [e for e in logs if e["event"] == "auto_pull_gate.calendar_coverage_exhausted"]
    assert len(warns) == 1
    assert warns[0]["date"] == "2027-01-04"


def test_gate_in_coverage_trading_day_no_coverage_warn() -> None:
    with capture_logs() as logs:
        v = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env={})
    assert v.allowed is True
    assert not [e for e in logs if e["event"] == "auto_pull_gate.calendar_coverage_exhausted"]


def test_gate_market_hours_belt_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The step-4 BELT is structurally unreachable while the settle floor holds
    (every in-window minute is already before_settle) — monkeypatch the guard
    predicate to prove a future settle-law change cannot open a hole."""
    monkeypatch.setattr(auto_pull_gate, "pull_allowed_default", lambda *, now: False)
    v = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env={})
    assert (v.allowed, v.reason) == (False, "market_hours_blocked")
    assert v.detail == "inside the NSE market-hours pull window — deferred"


def test_gate_default_clock_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """``now=None`` binds the live ``nse.now_ist`` (frozen here for determinism)."""
    monkeypatch.setattr(auto_pull_gate, "now_ist", lambda: _at(_TRADING_DAY, 18, 0))
    assert auto_pull_allowed(env={}).allowed is True


# ── muhurat_session_active ───────────────────────────────────────────────


def test_muhurat_session_active_margins() -> None:
    """B C5/C6: open − 30 min INCLUSIVE, close + 5 min EXCLUSIVE; the 2025
    AFTERNOON shape; the unlisted 2026 dates honestly False (never fabricated)."""
    evening = date(2024, 11, 1)  # 18:00-19:00
    assert muhurat_session_active(_at(evening, 17, 29, 59)) is False
    assert muhurat_session_active(_at(evening, 17, 30)) is True  # open − 30, inclusive
    assert muhurat_session_active(_at(evening, 18, 30)) is True
    assert muhurat_session_active(_at(evening, 19, 4, 59)) is True
    assert muhurat_session_active(_at(evening, 19, 5)) is False  # close + 5, exclusive
    afternoon = date(2025, 10, 21)  # 13:45-14:45 (the verified SEBI-2025 shift)
    assert muhurat_session_active(_at(afternoon, 13, 15)) is True
    assert muhurat_session_active(_at(afternoon, 13, 14, 59)) is False
    assert muhurat_session_active(_at(afternoon, 14, 49, 59)) is True
    assert muhurat_session_active(_at(afternoon, 14, 50)) is False
    # 2026: Diwali announced, timings NOT notified — honestly False all day.
    for d in (date(2026, 11, 8), date(2026, 11, 10)):
        for hh in range(0, 24, 3):
            assert muhurat_session_active(_at(d, hh, 0)) is False
    # A plain trading day has no session entry at all.
    assert muhurat_session_active(_at(_TRADING_DAY, 18, 15)) is False
    assert MUHURAT_PRE_MARGIN.total_seconds() == 30 * 60
    assert MUHURAT_POST_MARGIN.total_seconds() == 5 * 60


# ── compose_market_closed ────────────────────────────────────────────────


def test_compose_market_closed_none_passthrough() -> None:
    """Dry-run parity: a ``None`` base predicate composes to ``None``."""
    assert compose_market_closed(None) is None
    assert compose_market_closed(None, clock=lambda: _at(_TRADING_DAY, 12, 0)) is None


def test_compose_or_composition_with_clock() -> None:
    evening = date(2024, 11, 1)
    ticks = [_at(evening, 18, 30), _at(evening, 20, 0)]
    composed = compose_market_closed(lambda: False, clock=lambda: ticks.pop(0))
    assert composed is not None
    assert composed() is True  # base False, muhurat session active → blocked
    assert composed() is False  # base False, past close+5 → open


def test_compose_base_short_circuits() -> None:
    calls: list[str] = []

    def _clock() -> datetime:
        calls.append("tick")  # pragma: no cover — must never run
        return _at(_TRADING_DAY, 12, 0)

    composed = compose_market_closed(lambda: True, clock=_clock)
    assert composed is not None
    assert composed() is True
    assert calls == []  # the guard verdict short-circuits the muhurat probe


def test_compose_default_clock_binds_now_ist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_pull_gate, "now_ist", lambda: _at(date(2024, 11, 1), 18, 30))
    composed = compose_market_closed(lambda: False)
    assert composed is not None
    assert composed() is True

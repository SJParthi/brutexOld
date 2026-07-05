"""Unit tests for the observe-only partial-window truncation detector.

DEC-PARTIAL-WINDOW-DETECT-001. Proves the false-positive-safe classifier:

* a clearly-TRUNCATED SUCCESS window IS flagged;
* a SPARSE-but-complete (illiquid) window is NOT flagged;
* a LIVE-EDGE window is NOT flagged;
* an AT-EXPIRY window is NOT flagged;
* an EMPTY window is NOT flagged;
* the env-tunable tolerance / live-edge knobs behave + fall back safely.

100% line+branch on ``brutex/data/partial_window_detect.py`` (the dedicated
import-verified test the coverage-discovery gate requires for a NEW module).
"""

# Whitebox tests probe `_tunable_sessions`; silence the private-access warning.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from brutex.data import partial_window_detect as pwd
from brutex.data.partial_window_detect import (
    NOT_TRUNCATED,
    SUSPECTED_PARTIAL,
    SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT,
    SUSPECTED_PARTIAL_WINDOW_EVENT,
    PartialWindowVerdict,
    classify_partial_window,
    last_completed_session_on_or_before,
    observe_partial_window,
    sessions_between,
)
from structlog.testing import capture_logs

#: A frozen "now" well after every test window so only the live-edge knob (not a
#: real future) governs the live-edge exclusion. 2024-06-03 is a Monday.
_NOW = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)


def _ts(d: date) -> datetime:
    """A UTC bar timestamp landing on IST trading date *d* (09:30 IST = 04:00 UTC)."""
    # IST is UTC+5:30; a 09:30 IST bar is 04:00 UTC the same calendar day, so the IST
    # date the classifier derives is exactly *d*.
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC)


class TestSessionHelpers:
    def test_last_completed_session_on_a_trading_day_is_itself(self) -> None:
        # 2024-01-02 (Tue) is a trading day.
        assert last_completed_session_on_or_before(date(2024, 1, 2)) == date(2024, 1, 2)

    def test_last_completed_session_rolls_back_over_weekend(self) -> None:
        # 2024-01-07 is a Sunday → rolls back to Friday 2024-01-05.
        assert last_completed_session_on_or_before(date(2024, 1, 7)) == date(2024, 1, 5)

    def test_sessions_between_counts_only_trading_days(self) -> None:
        # Mon 2024-01-01 is a holiday (New Year); count sessions in (2024-01-01, 2024-01-05].
        # Trading days 02,03,04,05 → 4 sessions.
        assert sessions_between(date(2024, 1, 1), date(2024, 1, 5)) == 4

    def test_sessions_between_zero_when_end_not_after_start(self) -> None:
        assert sessions_between(date(2024, 1, 5), date(2024, 1, 5)) == 0
        assert sessions_between(date(2024, 1, 5), date(2024, 1, 2)) == 0


class TestClassifyPartialWindow:
    def test_truncated_window_is_flagged(self) -> None:
        # A 30-day window whose last bar lands on day ~2, contract still live well past it.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 4)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),  # still live long past the window
        )
        assert v.verdict == SUSPECTED_PARTIAL
        assert v.reason == "truncated"
        assert v.last_bar_date == date(2024, 3, 4)
        assert v.missing_sessions > 5
        assert v.is_suspected_partial()

    def test_complete_to_bound_not_flagged(self) -> None:
        # Last bar reaches the window's end boundary (end-exclusive → 04-01 last session).
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 4, 1)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "complete"
        assert v.missing_sessions == 0
        assert not v.is_suspected_partial()

    def test_sparse_but_complete_within_tolerance_not_flagged(self) -> None:
        # Last bar 2 sessions short of the bound (a holiday/publish-lag gap) → tolerated.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 28)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
            tolerance_sessions=5,
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "sparse_complete"
        assert 0 < v.missing_sessions <= 5

    def test_sparse_illiquid_few_bars_still_complete(self) -> None:
        # Only ONE bar in the whole window, but it lands AT the bound → COMPLETE, not flagged
        # (proves density is never consulted — the illiquid-option false-positive guard).
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 4, 1)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "complete"

    def test_live_edge_window_not_flagged(self) -> None:
        # Window end is "today" — within the live-edge horizon, vendor may not have published.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 5, 1)),
            window_end=date(2024, 6, 4),  # end-exclusive → 06-03 today (the live edge)
            now=_NOW,
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "live_edge"
        assert v.missing_sessions == 0

    def test_at_expiry_window_not_flagged(self) -> None:
        # Window end is the contract's expiry; the last bar reaches expiry → COMPLETE.
        # expiry 2024-03-28 (Thu); window asks past it but a dead contract has no later bar.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 28)),
            window_end=date(2024, 4, 30),
            now=_NOW,
            expiry=date(2024, 3, 28),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "at_expiry"
        assert v.expected_end_date == date(2024, 3, 28)

    def test_window_end_outside_calendar_coverage_not_flagged(self) -> None:
        # MAJOR-1: a window ENDING in an uncovered year (the year below the list
        # start — now 2019) abstains — the holiday list would miscount weekday
        # holidays as sessions → spurious flag. Honest abstention.
        from brutex.calendars.nse import HOLIDAY_COVERAGE_FIRST_YEAR

        uncovered_year = HOLIDAY_COVERAGE_FIRST_YEAR - 1
        v = classify_partial_window(
            last_bar_ts=_ts(date(uncovered_year, 3, 4)),
            window_end=date(uncovered_year, 4, 2),
            now=_NOW,
            expiry=date(2025, 1, 30),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "out_of_calendar_coverage"
        assert v.expected_end_date is None

    def test_last_bar_outside_calendar_coverage_not_flagged(self) -> None:
        # MAJOR-1: a covered window_end but a last bar in an uncovered year also abstains.
        from brutex.calendars.nse import HOLIDAY_COVERAGE_LAST_YEAR

        v = classify_partial_window(
            last_bar_ts=_ts(date(HOLIDAY_COVERAGE_LAST_YEAR + 1, 1, 6)),
            window_end=date(HOLIDAY_COVERAGE_LAST_YEAR + 1, 3, 1),
            now=datetime(HOLIDAY_COVERAGE_LAST_YEAR + 1, 6, 3, 10, 0, tzinfo=UTC),
            expiry=None,
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "out_of_calendar_coverage"

    def test_today_outside_calendar_coverage_not_flagged(self) -> None:
        # The today-anchor gap (calendar-correctness audit): the live-edge
        # horizon walks the calendar up to *now*, so a covered window judged
        # from an UNCOVERED current date (e.g. a 2099-pinned bench clock, or any
        # post-coverage year) would inflate the horizon count with unverifiable
        # "sessions" and bias toward a spurious flag. The classifier now
        # abstains honestly when TODAY itself is out of coverage — even for a
        # window that would otherwise be a confirmed truncation.
        from brutex.calendars.nse import HOLIDAY_COVERAGE_LAST_YEAR

        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 1, 3)),  # covered, materially truncated
            window_end=date(2024, 2, 1),  # covered
            now=datetime(HOLIDAY_COVERAGE_LAST_YEAR + 1, 6, 3, 10, 0, tzinfo=UTC),
            expiry=None,
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "out_of_calendar_coverage"

    def test_expiry_clamps_expected_end_but_still_truncated_before_it(self) -> None:
        # Expiry clamps the expected end, yet the last bar stops far BEFORE expiry → flagged.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 1, 3)),
            window_end=date(2024, 5, 1),
            now=_NOW,
            expiry=date(2024, 3, 28),  # expected_end clamps to expiry session, bar is months short
        )
        assert v.verdict == SUSPECTED_PARTIAL
        assert v.reason == "truncated"
        assert v.expected_end_date == date(2024, 3, 28)

    def test_empty_window_not_flagged(self) -> None:
        v = classify_partial_window(
            last_bar_ts=None,
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == NOT_TRUNCATED
        assert v.reason == "empty"
        assert v.last_bar_date is None
        assert v.expected_end_date is None

    def test_no_expiry_spot_series_truncation_flagged(self) -> None:
        # A spot series (expiry=None): the end clamps only to today; still truncated.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 4)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=None,
        )
        assert v.verdict == SUSPECTED_PARTIAL
        assert v.reason == "truncated"

    def test_window_end_past_today_clamps_to_today(self) -> None:
        # A window_end far in the future clamps to today's session; a bar reaching today's
        # session is complete (not flagged) — and within live-edge anyway.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 5, 31)),  # Fri before the Mon "today"
            window_end=date(2025, 1, 1),
            now=_NOW,
            expiry=None,
        )
        assert v.verdict == NOT_TRUNCATED

    def test_tolerance_override_makes_small_gap_trip(self) -> None:
        # With tolerance 0, even a 1-session gap is a truncation (and not at the live edge).
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 28)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
            tolerance_sessions=0,
            live_edge_sessions=1,
        )
        assert v.verdict == SUSPECTED_PARTIAL
        assert v.reason == "truncated"

    def test_live_edge_override_widens_exclusion(self) -> None:
        # A window ending ~10 sessions before today is normally flagged, but a huge live-edge
        # horizon excludes it.
        kwargs = {
            "last_bar_ts": _ts(date(2024, 3, 4)),
            "window_end": date(2024, 5, 17),
            "now": _NOW,
            "expiry": date(2024, 12, 31),
        }
        flagged = classify_partial_window(**kwargs, live_edge_sessions=1)  # type: ignore[arg-type]
        assert flagged.verdict == SUSPECTED_PARTIAL
        excluded = classify_partial_window(**kwargs, live_edge_sessions=500)  # type: ignore[arg-type]
        assert excluded.verdict == NOT_TRUNCATED
        assert excluded.reason == "live_edge"

    def test_non_utc_now_and_bar_are_normalised_to_ist(self) -> None:
        # A bar + now in a non-UTC tz are still classified on the IST grid (astimezone path).
        plus2 = timezone(timedelta(hours=2))
        v = classify_partial_window(
            last_bar_ts=datetime(2024, 4, 1, 6, 0, tzinfo=plus2),
            window_end=date(2024, 4, 2),
            now=datetime(2024, 6, 3, 6, 30, tzinfo=plus2),
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == NOT_TRUNCATED


class TestEnvTunables:
    def test_env_tolerance_used_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", "1")
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_LIVE_EDGE_SESSIONS", "1")
        # 2-session gap > env tolerance 1 → flagged.
        v = classify_partial_window(
            last_bar_ts=_ts(date(2024, 3, 27)),
            window_end=date(2024, 4, 2),
            now=_NOW,
            expiry=date(2024, 12, 31),
        )
        assert v.verdict == SUSPECTED_PARTIAL

    def test_env_missing_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", raising=False)
        assert pwd._tunable_sessions("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", 7) == 7

    def test_env_malformed_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", "not-an-int")
        assert pwd._tunable_sessions("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", 5) == 5

    def test_env_non_positive_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", "0")
        assert pwd._tunable_sessions("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", 5) == 5
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", "-3")
        assert pwd._tunable_sessions("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", 5) == 5

    def test_env_positive_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", "9")
        assert pwd._tunable_sessions("BRUTEX_PARTIAL_WINDOW_TOLERANCE_SESSIONS", 5) == 9


def test_verdict_is_frozen_immutable() -> None:
    v = PartialWindowVerdict(
        verdict=NOT_TRUNCATED,
        last_bar_date=None,
        expected_end_date=None,
        missing_sessions=0,
        reason="empty",
    )
    with pytest.raises((AttributeError, Exception)):
        v.verdict = SUSPECTED_PARTIAL  # type: ignore[misc]


class _Logger:
    """A minimal structlog-shaped recorder for the COMMON observe helper."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class TestObservePartialWindow:
    """The COMMON shared wiring helper both pull spines call (DEC-PARTIAL-WINDOW-DETECT-001)."""

    def test_truncation_emits_one_warning(self) -> None:
        log = _Logger()
        observe_partial_window(
            log,
            last_bar_ts=_ts(date(2024, 1, 8)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=date(2025, 1, 30),
            symbol="NSE-NIFTY-30Mar24-FUT",
            interval="1day",
            pull_id="pid-1",
        )
        warns = [e for e in log.events if e[0] == SUSPECTED_PARTIAL_WINDOW_EVENT]
        assert len(warns) == 1
        fields = warns[0][1]
        assert fields["symbol"] == repr("NSE-NIFTY-30Mar24-FUT")
        assert fields["last_bar_date"] == "2024-01-08"
        assert fields["expiry"] == "2025-01-30"
        # Also assert it fires through a REAL structlog logger (the production shape).
        import structlog

        with capture_logs() as logs:
            observe_partial_window(
                structlog.get_logger("t"),
                last_bar_ts=_ts(date(2024, 1, 8)),
                window_end=date(2024, 3, 1),
                now=_NOW,
                expiry=None,
                symbol="NSE-NIFTY",
                interval="1minute",
                pull_id=None,
            )
        assert any(r["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT for r in logs)

    def test_complete_window_silent(self) -> None:
        log = _Logger()
        observe_partial_window(
            log,
            last_bar_ts=_ts(date(2024, 2, 29)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1day",
            pull_id="pid-1",
        )
        assert [e for e in log.events if e[0] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_empty_window_silent(self) -> None:
        log = _Logger()
        observe_partial_window(
            log,
            last_bar_ts=None,
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1day",
            pull_id="pid-1",
        )
        assert log.events == []

    def test_diagnostic_error_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_k: object) -> object:
            raise RuntimeError("classify blew up")

        monkeypatch.setattr(pwd, "classify_partial_window", _boom)
        log = _Logger()
        observe_partial_window(
            log,
            last_bar_ts=_ts(date(2024, 1, 8)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1day",
            pull_id="pid-1",
        )
        assert [e for e in log.events if e[0] == SUSPECTED_PARTIAL_WINDOW_ERROR_EVENT]
        assert [e for e in log.events if e[0] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

    def test_returns_truncation_verdict_for_caller_demotion(self) -> None:
        # DEC-PARTIAL-WINDOW-REPULL-001: the helper now RETURNS the verdict so a caller can
        # demote a truncated SUCCESS window to a re-pullable ledger status (beyond the WARNING).
        log = _Logger()
        verdict = observe_partial_window(
            log,
            last_bar_ts=_ts(date(2024, 1, 8)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1minute",
            pull_id="pid-1",
        )
        assert verdict is not None and verdict.is_suspected_partial()

    def test_returns_not_truncated_verdict_for_complete_window(self) -> None:
        log = _Logger()
        verdict = observe_partial_window(
            log,
            last_bar_ts=_ts(date(2024, 2, 29)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1minute",
            pull_id="pid-1",
        )
        assert verdict is not None and not verdict.is_suspected_partial()

    def test_returns_none_when_diagnostic_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_k: object) -> object:
            raise RuntimeError("classify blew up")

        monkeypatch.setattr(pwd, "classify_partial_window", _boom)
        verdict = observe_partial_window(
            _Logger(),
            last_bar_ts=_ts(date(2024, 1, 8)),
            window_end=date(2024, 3, 1),
            now=_NOW,
            expiry=None,
            symbol="NSE-NIFTY",
            interval="1minute",
            pull_id="pid-1",
        )
        assert verdict is None  # a guarded failure never demotes


# ─────────── observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001) ───────────


class TestObservedOverlayMargin:
    """The overlay corrects the truncation MARGIN: an observed holiday is not a lag."""

    _NOW = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)

    def test_sessions_between_observed(self) -> None:
        # (2026-05-05, 2026-05-14] holds 7 trading sessions; overlaying two of
        # them removes exactly those from the count (remove-only).
        base = sessions_between(date(2026, 5, 5), date(2026, 5, 14))
        assert base == 7
        shrunk = sessions_between(
            date(2026, 5, 5),
            date(2026, 5, 14),
            observed=frozenset({date(2026, 5, 6), date(2026, 5, 7)}),
        )
        assert shrunk == 5

    def test_last_completed_session_observed(self) -> None:
        # 2026-05-14 (Thu) is a session; overlaying it rolls the expected end
        # back to 2026-05-13.
        assert last_completed_session_on_or_before(date(2026, 5, 14)) == date(2026, 5, 14)
        assert last_completed_session_on_or_before(
            date(2026, 5, 14), observed=frozenset({date(2026, 5, 14)})
        ) == date(2026, 5, 13)

    def test_classify_partial_window_observed_margin(self) -> None:
        # FAIL-BEFORE/PASS-AFTER: last bar 2026-05-05, window end 2026-05-15 ->
        # 7 missing sessions > 5 -> SUSPECTED_PARTIAL. Overlaying two of the
        # "missing" sessions shrinks the lag to 5 <= tolerance -> NOT_TRUNCATED.
        last_bar = datetime(2026, 5, 5, 4, 5, tzinfo=UTC)
        flagged = classify_partial_window(
            last_bar_ts=last_bar,
            window_end=date(2026, 5, 15),
            now=self._NOW,
            expiry=None,
        )
        assert flagged.verdict == SUSPECTED_PARTIAL
        assert flagged.missing_sessions == 7
        cleared = classify_partial_window(
            last_bar_ts=last_bar,
            window_end=date(2026, 5, 15),
            now=self._NOW,
            expiry=None,
            observed=frozenset({date(2026, 5, 6), date(2026, 5, 7)}),
        )
        assert cleared.verdict == NOT_TRUNCATED
        assert cleared.missing_sessions == 5
        assert cleared.reason == "sparse_complete"

    def test_default_empty_overlay_byte_identical(self) -> None:
        last_bar = datetime(2026, 5, 5, 4, 5, tzinfo=UTC)
        default = classify_partial_window(
            last_bar_ts=last_bar, window_end=date(2026, 5, 15), now=self._NOW, expiry=None
        )
        explicit = classify_partial_window(
            last_bar_ts=last_bar,
            window_end=date(2026, 5, 15),
            now=self._NOW,
            expiry=None,
            observed=frozenset(),
        )
        assert default == explicit

    def test_observe_threads_observed(self) -> None:
        import structlog

        with capture_logs() as logs:
            verdict = observe_partial_window(
                structlog.get_logger("test"),
                last_bar_ts=datetime(2026, 5, 5, 4, 5, tzinfo=UTC),
                window_end=date(2026, 5, 15),
                now=self._NOW,
                expiry=None,
                symbol="NIFTY",
                interval="1minute",
                pull_id=None,
                observed=frozenset({date(2026, 5, 6), date(2026, 5, 7)}),
            )
        assert verdict is not None
        assert verdict.verdict == NOT_TRUNCATED
        assert [e for e in logs if e["event"] == SUSPECTED_PARTIAL_WINDOW_EVENT] == []

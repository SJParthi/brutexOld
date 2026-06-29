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
    PartialWindowVerdict,
    classify_partial_window,
    last_completed_session_on_or_before,
    sessions_between,
)

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

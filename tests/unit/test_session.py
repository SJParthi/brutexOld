"""Tests for `brutex.data.session` — NSE/BSE session filter (09:15–15:30 IST).

100% line+branch: the open-inclusive / close-exclusive boundaries, the legacy
date-blind intraday filter (keeps in-session, drops pre-open + post-close), and
the calendar-aware :func:`filter_to_trading_session`
(DEC-INGEST-BOUNDARY-INTEGRITY-001): the regular-day golden parity vs the legacy
filter, the muhurat-evening KEEP (a sanctioned behavior change), the
special-weekend KEEP, the non-trading-date DROP, and the out-of-coverage
fixed-window abstention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from brutex.calendars.nse import get_session
from brutex.data.models import CandleBar
from brutex.data.session import (
    ALL_FILTERED,
    ALL_FILTERED_WINDOW_EVENT,
    SessionFilterResult,
    filter_intraday_session,
    filter_to_trading_session,
    format_ist_clock,
    in_session,
    is_all_filtered,
    is_session_exempt,
    observe_all_filtered,
)
from hypothesis import given
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")


def _bar(hh: int, mm: int) -> CandleBar:
    """A bar whose UTC timestamp equals ``hh:mm`` UTC on 2021-01-04."""
    return CandleBar(
        timestamp=datetime(2021, 1, 4, hh, mm, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


def _ist_bar(year: int, month: int, day: int, hh: int, mm: int) -> CandleBar:
    """A bar whose timestamp is the given IST wall clock (stored UTC, as the lake does)."""
    ts = datetime(year, month, day, hh, mm, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


# IST = UTC + 5:30 → 09:15 IST = 03:45 UTC; 15:30 IST = 10:00 UTC.
class TestInSession:
    @pytest.mark.parametrize(
        ("utc_h", "utc_m", "expected"),
        [
            (3, 45, True),  # 09:15 IST — open, inclusive
            (3, 44, False),  # 09:14 IST — pre-open
            (3, 30, False),  # 09:00 IST — pre-open auction
            (6, 0, True),  # 11:30 IST — mid-session
            (9, 59, True),  # 15:29 IST — last intraday minute
            (10, 0, False),  # 15:30 IST — close, exclusive
            (10, 1, False),  # 15:31 IST — post-close
        ],
    )
    def test_boundaries(self, utc_h: int, utc_m: int, expected: bool) -> None:
        assert in_session(_bar(utc_h, utc_m).timestamp) is expected


class TestFilterIntradaySession:
    def test_keeps_in_session_drops_out(self) -> None:
        bars = [_bar(3, 30), _bar(3, 45), _bar(6, 0), _bar(9, 59), _bar(10, 0)]
        kept = filter_intraday_session(bars)
        assert [b.timestamp.hour * 60 + b.timestamp.minute for b in kept] == [
            3 * 60 + 45,
            6 * 60,
            9 * 60 + 59,
        ]

    def test_empty(self) -> None:
        assert filter_intraday_session([]) == []


class TestFilterToTradingSession:
    """Calendar-aware boundary filter (DEC-INGEST-BOUNDARY-INTEGRITY-001)."""

    def test_golden_regular_day_parity_with_legacy_filter(self) -> None:
        # GOLDEN: on a REGULAR trading day (2024-01-02, Tuesday) the calendar-aware
        # kept-set is byte-identical to the legacy fixed-window filter's kept-set —
        # every minute 08:00–16:59 IST, pinning the same [09:15, 15:30) inclusivity.
        bars = [_ist_bar(2024, 1, 2, h, m) for h in range(8, 17) for m in range(60)]
        verdict = filter_to_trading_session(bars)
        legacy = filter_intraday_session(bars)
        assert verdict.kept == legacy
        assert len(verdict.kept) == 375  # the full 09:15–15:29 session
        assert verdict.dropped_non_trading_date == 0
        assert verdict.dropped_off_session == len(bars) - 375

    def test_muhurat_evening_bar_is_kept(self) -> None:
        # SANCTIONED BEHAVIOR CHANGE: an evening Diwali-muhurat bar (2024-11-01
        # session 18:00–19:00 IST) is now KEPT — the date-blind filter wrongly
        # dropped it.
        evening = _ist_bar(2024, 11, 1, 18, 30)
        assert filter_intraday_session([evening]) == []  # the old defect, pinned
        verdict = filter_to_trading_session([evening])
        assert verdict.kept == [evening]
        assert verdict.dropped == 0

    def test_muhurat_session_boundaries_open_inclusive_close_exclusive(self) -> None:
        at_open = _ist_bar(2024, 11, 1, 18, 0)
        at_close = _ist_bar(2024, 11, 1, 19, 0)
        verdict = filter_to_trading_session([at_open, at_close])
        assert verdict.kept == [at_open]  # open inclusive, close exclusive
        assert verdict.dropped_off_session == 1

    def test_regular_hours_bar_on_a_muhurat_date_is_dropped(self) -> None:
        # On Diwali the exchange trades ONLY the muhurat hour — a 10:00 IST bar on
        # that date is vendor junk and is dropped (counted off-session).
        verdict = filter_to_trading_session([_ist_bar(2024, 11, 1, 10, 0)])
        assert verdict.kept == []
        assert verdict.dropped_off_session == 1
        assert verdict.all_filtered is True

    def test_special_weekend_session_bar_is_kept(self) -> None:
        # 2025-02-01 (Saturday) was a Budget special session 09:15–15:30 — KEPT.
        saturday_budget = _ist_bar(2025, 2, 1, 10, 0)
        verdict = filter_to_trading_session([saturday_budget])
        assert verdict.kept == [saturday_budget]
        assert verdict.dropped == 0

    def test_extended_session_bar_2021_02_24_is_kept(self) -> None:
        # SANCTIONED BEHAVIOR CHANGE (DEC-EXTENDED-SESSION-OUTAGE-001): a real
        # 16:30 IST bar on the 2021-02-24 NSE-outage EXTENDED day (close 17:00
        # at NSE + BSE + MSEI per the SEBI press releases, verified 2026-07-11)
        # is now KEPT — the pre-fix resolver fabricated a 15:30 close and
        # silently dropped it. FAILS on the pre-fix source (the reversion gate).
        extended_bar = _ist_bar(2021, 2, 24, 16, 30)
        assert filter_intraday_session([extended_bar]) == []  # the old defect, pinned
        verdict = filter_to_trading_session([extended_bar])
        assert verdict.kept == [extended_bar]
        assert verdict.dropped == 0

    def test_extended_session_close_is_half_open_at_1700(self) -> None:
        # [open, close): 16:59 is the last kept minute; 17:00:00 drops.
        last_minute = _ist_bar(2021, 2, 24, 16, 59)
        at_close = _ist_bar(2021, 2, 24, 17, 0)
        verdict = filter_to_trading_session([last_minute, at_close])
        assert verdict.kept == [last_minute]
        assert verdict.dropped_off_session == 1

    def test_extended_hours_bar_on_a_neighbor_date_still_drops(self) -> None:
        # The extension is ONE date only — a 15:45 bar on the EVE (2021-02-23,
        # a regular Tuesday) stays post-close junk and drops.
        verdict = filter_to_trading_session([_ist_bar(2021, 2, 23, 15, 45)])
        assert verdict.kept == []
        assert verdict.dropped_off_session == 1

    def test_circuit_halt_day_bars_kept_unchanged(self) -> None:
        # DEC-HALT-DAY-TREATMENT-001 — ANNOTATE, never drop: the 2020-03-23
        # market-wide-halt day stays a full REGULAR session; its real bars
        # (including one printed inside the 09:58→~10:58 halt window's wall
        # clock — the filter judges [open, close) only, never intra-day halts)
        # pass the boundary exactly as before.
        in_session = _ist_bar(2020, 3, 23, 10, 30)
        post_close = _ist_bar(2020, 3, 23, 15, 45)
        verdict = filter_to_trading_session([in_session, post_close])
        assert verdict.kept == [in_session]
        assert verdict.dropped_off_session == 1
        assert verdict.dropped_non_trading_date == 0

    def test_pre_open_bar_is_dropped_and_counted(self) -> None:
        verdict = filter_to_trading_session([_ist_bar(2024, 1, 2, 9, 0)])
        assert verdict.kept == []
        assert verdict.dropped_off_session == 1
        assert verdict.dropped == 1

    def test_holiday_bar_is_dropped_as_non_trading_date(self) -> None:
        # 2024-01-26 (Republic Day, a Friday) — in-window wall clock, NO session:
        # the date-blind filter wrongly KEPT it; now dropped + counted.
        holiday_bar = _ist_bar(2024, 1, 26, 10, 0)
        assert filter_intraday_session([holiday_bar]) == [holiday_bar]  # old defect
        verdict = filter_to_trading_session([holiday_bar])
        assert verdict.kept == []
        assert verdict.dropped_non_trading_date == 1
        assert verdict.all_filtered is True

    def test_weekend_bar_is_dropped_as_non_trading_date(self) -> None:
        verdict = filter_to_trading_session([_ist_bar(2024, 1, 6, 10, 0)])  # Saturday
        assert verdict.kept == []
        assert verdict.dropped_non_trading_date == 1

    def test_out_of_coverage_date_abstains_to_fixed_window(self) -> None:
        # 2027 is past the published holiday-list span — the filter ABSTAINS from
        # the calendar and falls back to the legacy fixed [09:15, 15:30) window,
        # byte-identical to the old filter for those dates (Golden Rule #1).
        in_window = _ist_bar(2027, 1, 4, 10, 0)  # Monday, in-window
        out_window = _ist_bar(2027, 1, 4, 17, 30)
        verdict = filter_to_trading_session([in_window, out_window])
        assert verdict.kept == [in_window]
        assert verdict.dropped_off_session == 1
        assert verdict.dropped_non_trading_date == 0

    def test_session_lookup_is_cached_per_unique_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # O(bars + unique_dates): many bars on ONE date resolve the session ONCE.
        # The real function is taken from its home module (brutex.calendars.nse —
        # the identical object brutex.data.session binds); the patch targets the
        # session module's binding by dotted path, so this file imports the module
        # in exactly ONE style (CodeQL py/import-and-import-from, alert #534).
        calls = {"n": 0}

        def _counting(d: object) -> object:
            calls["n"] += 1
            return get_session(d)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.session.get_session", _counting)
        bars = [_ist_bar(2024, 1, 2, 10, m) for m in range(30)]
        verdict = filter_to_trading_session(bars)
        assert len(verdict.kept) == 30
        assert calls["n"] == 1  # one lookup for the one unique date

    def test_empty_input(self) -> None:
        verdict = filter_to_trading_session([])
        assert verdict.kept == []
        assert verdict.dropped == 0
        assert verdict.all_filtered is False  # nothing came in — NOT all-filtered

    def test_all_filtered_constant_and_result_shape(self) -> None:
        assert ALL_FILTERED == "ALL_FILTERED"
        kept_bar = _ist_bar(2024, 1, 2, 10, 0)
        verdict = SessionFilterResult(kept=[kept_bar], dropped_off_session=2)
        assert verdict.dropped == 2
        assert verdict.all_filtered is False  # a survivor → never all-filtered


class _SpyLog:
    """Duck-typed structlog spy — records every ``.warning(event, **fields)`` call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class TestAllFilteredBoundary:
    """`is_all_filtered` / `observe_all_filtered` — the WHOLE-boundary discriminator
    (DEC-DATA-HONESTY-ALLFILTERED-SEED-001): raw > 0 with ZERO kept bars after the
    session filter AND the future-bar drop."""

    @pytest.mark.parametrize(
        ("raw", "kept", "expected"),
        [
            (3, 0, True),  # the all-filtered shape
            (1, 0, True),
            (0, 0, False),  # genuine EMPTY — untouched channel
            (3, 1, False),  # a survivor → never all-filtered
            (3, 3, False),
        ],
    )
    def test_is_all_filtered_truth_table(self, raw: int, kept: int, expected: bool) -> None:
        assert is_all_filtered(raw, kept) is expected

    @given(
        raw=st.integers(min_value=0, max_value=10_000),
        kept=st.integers(min_value=0, max_value=10_000),
    )
    def test_property_all_filtered_iff_raw_positive_and_kept_zero(
        self, raw: int, kept: int
    ) -> None:
        # The exact predicate law: True ⇔ raw > 0 ∧ kept == 0. Pure arithmetic —
        # never keyed on dedup counts, error types, or gates.
        assert is_all_filtered(raw, kept) is (raw > 0 and kept == 0)

    def test_observe_all_filtered_warns_with_fields(self) -> None:
        log = _SpyLog()
        verdict = observe_all_filtered(
            log,
            raw_count=4,
            kept_count=0,
            pull_id="p-1",
            fields={"window_start": "2024-01-01", "window_end": "2024-01-31", "symbol": "NIFTY"},
        )
        assert verdict is True
        assert len(log.events) == 1
        event, fields = log.events[0]
        assert event == ALL_FILTERED_WINDOW_EVENT == "ingest.window.all_bars_filtered"
        assert fields["raw_bars"] == 4
        assert fields["kept_bars"] == 0
        assert fields["pull_id"] == "p-1"
        assert fields["window_start"] == "2024-01-01"
        assert fields["window_end"] == "2024-01-31"
        assert fields["symbol"] == "NIFTY"

    def test_observe_all_filtered_silent_when_bars_kept(self) -> None:
        log = _SpyLog()
        assert (
            observe_all_filtered(log, raw_count=4, kept_count=2, pull_id=None, fields={}) is False
        )
        assert log.events == []

    def test_observe_all_filtered_silent_on_genuine_empty(self) -> None:
        # raw == 0 is the genuine-EMPTY channel — never warned here (the EMPTY
        # ledger path + its own detectors own that shape).
        log = _SpyLog()
        assert (
            observe_all_filtered(log, raw_count=0, kept_count=0, pull_id=None, fields={}) is False
        )
        assert log.events == []


class TestSessionExempt:
    """GIFT Nifty is exempt from the NSE 09:15–15:30 filter (DEC-DHAN-GIFTNIFTY-SESSION-001)."""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("GIFTNIFTY", True),
            ("giftnifty", True),  # case-insensitive
            ("  GIFTNIFTY  ", True),  # whitespace-tolerant
            ("NIFTY", False),
            ("BANKNIFTY", False),
            ("SENSEX", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_session_exempt(self, symbol: str | None, expected: bool) -> None:
        assert is_session_exempt(symbol) is expected


class TestFormatIstClock:
    """`format_ist_clock` — the COMMON UTC→clean-IST display rule (DEC-WINDOW-LEDGER-001)."""

    def test_utc_iso_string_converts_to_clean_ist(self) -> None:
        # 08:35:53 UTC + 5:30 = 14:05:53 IST; the +05:30 offset is dropped.
        assert format_ist_clock("2026-06-09T08:35:53.763513+00:00") == "2026-06-09 14:05:53"

    def test_utc_datetime_converts_to_clean_ist(self) -> None:
        assert (
            format_ist_clock(datetime(2026, 6, 9, 8, 35, 53, tzinfo=UTC)) == "2026-06-09 14:05:53"
        )

    def test_none_passes_through(self) -> None:
        assert format_ist_clock(None) is None

    def test_naive_datetime_assumed_ist(self) -> None:
        # No tzinfo → assumed already-IST, just formatted (no shift).
        assert format_ist_clock(datetime(2026, 6, 9, 14, 5, 53)) == "2026-06-09 14:05:53"

    def test_unparseable_string_returned_unchanged(self) -> None:
        # A free-text note (e.g. last_error) is not a timestamp — left as-is.
        assert format_ist_clock("DhanRateLimitError") == "DhanRateLimitError"

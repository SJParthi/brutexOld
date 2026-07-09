"""Unit tests for the missing-trading-day gap list (DEC-COVERAGE-MISSING-SESSIONS-001).

Pure-module coverage of :mod:`brutex.dashboard.coverage_missing`: the exact-missing-set law
(holidays / muhurat / weekends handled, no off-by-one, no timezone drift), the O(sessions)
bound, every abstain branch, and the payload builder's filters. Offline — no lake, no store.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.calendars import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    MUHURAT_SESSIONS,
    is_holiday,
    is_trading_day,
    trading_days_between,
)
from brutex.dashboard.coverage_missing import (
    ABSTAIN_BAD_RANGE_OVERRIDE,
    ABSTAIN_NO_RANGE,
    ABSTAIN_NO_SESSIONS,
    ABSTAIN_NO_STORED_DATES,
    ABSTAIN_OUT_OF_COVERAGE,
    DRILL_INTERVAL,
    MAX_MISSING_DATES,
    MissingSessions,
    build_missing_sessions_payload,
    compute_missing_sessions,
)


def _all_present(exchange: str, symbol: str, first: str, last: str) -> MissingSessions:
    """Helper: audit a span whose stored set == every expected session (zero gaps)."""
    expected = trading_days_between(date.fromisoformat(first), date.fromisoformat(last))
    return compute_missing_sessions(
        exchange, "INDEX", symbol, DRILL_INTERVAL, first, last, frozenset(expected)
    )


class TestComputeMissingSessions:
    def test_exact_missing_set_one_hole(self) -> None:
        # 2024-01-01 (Mon) is an NSE New Year holiday; 02..05 are trading days.
        # Store 02, 04, 05 → EXACTLY 03-Jan (Wed) must be reported missing.
        first, last = "2024-01-02", "2024-01-05"
        stored = frozenset({date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)})
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, first, last, stored
        )
        assert not result.abstained
        assert result.missing_dates == ("2024-01-03",)
        assert result.missing_count == 1
        assert result.expected_count == 4  # 02,03,04,05 all weekday non-holidays
        assert result.present_count == 3

    def test_weekend_never_expected_no_off_by_one(self) -> None:
        # Fri 2024-01-05 → Mon 2024-01-08. The Sat/Sun in between are NOT sessions,
        # so an all-stored weekday set yields ZERO missing (no weekend false-positive).
        result = _all_present("NSE", "NIFTY", "2024-01-05", "2024-01-08")
        assert result.missing_dates == ()
        assert result.missing_count == 0
        # Only the 3 weekday sessions (Fri, Mon) minus the weekend → 2 sessions.
        assert result.expected_count == 2

    def test_holiday_never_flagged_missing(self) -> None:
        # 2024-01-26 (Republic Day) is an NSE holiday inside the span; storing only
        # the surrounding trading days must NOT flag the holiday as missing.
        assert is_holiday(date(2024, 1, 26))
        first, last = "2024-01-25", "2024-01-29"
        expected = trading_days_between(date.fromisoformat(first), date.fromisoformat(last))
        assert date(2024, 1, 26) not in expected
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, first, last, frozenset(expected)
        )
        assert result.missing_dates == ()

    def test_muhurat_date_is_holiday_never_expected(self) -> None:
        # A muhurat evening session falls on a Diwali HOLIDAY, so is_trading_day is
        # False and it is never in the expected set — a stored muhurat bar is surplus,
        # never a "missing" flag (the honest §7 muhurat handling).
        muhurat = next(iter(MUHURAT_SESSIONS))
        assert not is_trading_day(muhurat)
        # Audit a span containing the muhurat date, storing NOTHING on it.
        span_start = date(muhurat.year, muhurat.month, 1)
        span_end = date(muhurat.year, muhurat.month, 28)
        expected = trading_days_between(span_start, span_end)
        assert muhurat not in expected
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            span_start.isoformat(),
            span_end.isoformat(),
            frozenset(expected),  # every real session stored; muhurat absent
        )
        assert result.missing_dates == ()  # muhurat NOT flagged missing

    def test_missing_dates_sorted_ascending_no_tz_drift(self) -> None:
        # Store nothing → every expected session is missing, in ascending ISO order.
        first, last = "2024-03-01", "2024-03-15"
        expected = trading_days_between(date.fromisoformat(first), date.fromisoformat(last))
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, first, last, frozenset()
        )
        assert list(result.missing_dates) == [d.isoformat() for d in expected]
        assert result.missing_dates == tuple(sorted(result.missing_dates))
        assert result.present_count == 0
        assert result.missing_count == result.expected_count == len(expected)

    def test_bse_exchange_uses_bse_holidays(self) -> None:
        # SENSEX (BSE) exchange threads through to the BSE holiday set — the call
        # must not raise and must abstain/compute on the BSE calendar.
        first, last = "2024-02-01", "2024-02-29"
        expected = trading_days_between(
            date.fromisoformat(first), date.fromisoformat(last), exchange="BSE"
        )
        result = compute_missing_sessions(
            "BSE", "INDEX", "SENSEX", DRILL_INTERVAL, first, last, frozenset(expected)
        )
        assert not result.abstained
        assert result.missing_count == 0

    def test_unknown_exchange_falls_back_to_shared_holidays(self) -> None:
        # A non NSE/BSE exchange must NOT raise a ValueError deep in the audit.
        result = _all_present("MCX", "X", "2024-01-02", "2024-01-05")
        assert not result.abstained

    def test_range_override_audits_wider_span(self) -> None:
        # Stored span is one week, but the operator audits the whole month via the
        # explicit range override → the un-stored earlier sessions are surfaced.
        stored = frozenset(trading_days_between(date(2024, 1, 22), date(2024, 1, 26)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-22",
            "2024-01-26",
            stored,
            range_start="2024-01-01",
            range_end="2024-01-31",
        )
        assert result.range_start == "2024-01-01"
        assert result.range_end == "2024-01-31"
        assert result.missing_count and result.missing_count > 0
        # None of the stored week's sessions are missing.
        assert "2024-01-22" not in result.missing_dates

    def test_truncation_flagged_when_over_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("brutex.dashboard.coverage_missing.MAX_MISSING_DATES", 3)
        first, last = "2024-01-02", "2024-01-31"  # > 3 sessions, none stored
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, first, last, frozenset()
        )
        assert result.missing_truncated
        assert len(result.missing_dates) == 3
        assert result.missing_count is not None and result.missing_count > 3

    def test_no_truncation_under_default_cap(self) -> None:
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-02", "2024-01-31", frozenset()
        )
        assert not result.missing_truncated
        assert result.missing_count is not None
        assert result.missing_count <= MAX_MISSING_DATES

    # ── abstain branches ──
    def test_abstain_no_stored_dates(self) -> None:
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-02", "2024-01-05", None
        )
        assert result.abstained
        assert result.abstain_reason == ABSTAIN_NO_STORED_DATES
        assert result.missing_dates == ()
        assert result.expected_count is None and result.present_count is None

    def test_abstain_missing_first_date(self) -> None:
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, None, "2024-01-05", frozenset()
        )
        assert result.abstained and result.abstain_reason == ABSTAIN_NO_RANGE

    def test_abstain_unparseable_date(self) -> None:
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "not-a-date", "2024-01-05", frozenset()
        )
        assert result.abstained and result.abstain_reason == ABSTAIN_NO_RANGE

    def test_abstain_reversed_range(self) -> None:
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-05", "2024-01-02", frozenset()
        )
        assert result.abstained and result.abstain_reason == ABSTAIN_NO_RANGE

    def test_abstain_below_holiday_coverage(self) -> None:
        low_year = HOLIDAY_COVERAGE_FIRST_YEAR - 1
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            f"{low_year}-01-02",
            f"{low_year}-01-05",
            frozenset(),
        )
        assert result.abstained and result.abstain_reason == ABSTAIN_OUT_OF_COVERAGE

    def test_abstain_above_holiday_coverage(self) -> None:
        high_year = HOLIDAY_COVERAGE_LAST_YEAR + 1
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            f"{high_year}-01-02",
            f"{high_year}-01-05",
            frozenset(),
        )
        assert result.abstained and result.abstain_reason == ABSTAIN_OUT_OF_COVERAGE

    def test_abstain_zero_session_span(self) -> None:
        # A single weekend day is not a trading day → expected is empty → abstain.
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-06", "2024-01-06", frozenset()
        )  # 2024-01-06 is a Saturday
        assert date(2024, 1, 6).weekday() == 5
        assert result.abstained and result.abstain_reason == ABSTAIN_NO_SESSIONS

    def test_to_dict_shape(self) -> None:
        d = _all_present("NSE", "NIFTY", "2024-01-02", "2024-01-05").to_dict()
        assert set(d) == {
            "exchange",
            "segment",
            "symbol",
            "interval",
            "range_start",
            "range_end",
            "expected_count",
            "present_count",
            "missing_count",
            "missing_dates",
            "missing_truncated",
            "abstained",
            "abstain_reason",
        }
        assert isinstance(d["missing_dates"], list)


def _row(
    symbol: str, interval: str, first: str | None, last: str | None, dates: object
) -> dict[str, object]:
    return {
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": symbol,
        "interval": interval,
        "first_date": first,
        "last_date": last,
        "session_dates": dates,
    }


class TestBuildMissingSessionsPayload:
    def test_empty_rows_well_shaped(self) -> None:
        payload = build_missing_sessions_payload([])
        assert payload["datasets"] == 0
        assert payload["total_missing"] == 0
        assert payload["rows"] == []
        assert payload["interval"] == DRILL_INTERVAL

    def test_only_fetched_interval_audited(self) -> None:
        rows = [
            _row("NIFTY", "1minute", "2024-01-02", "2024-01-05", frozenset()),
            _row("NIFTY", "1day", "2024-01-02", "2024-01-05", frozenset()),  # derived → skipped
        ]
        payload = build_missing_sessions_payload(rows)
        assert payload["datasets"] == 1
        assert payload["rows"][0]["interval"] == "1minute"

    def test_symbol_and_exchange_filter_case_insensitive(self) -> None:
        rows = [
            _row("NIFTY", "1minute", "2024-01-02", "2024-01-05", frozenset()),
            _row("BANKNIFTY", "1minute", "2024-01-02", "2024-01-05", frozenset()),
        ]
        payload = build_missing_sessions_payload(rows, symbol="nifty")
        assert payload["datasets"] == 1
        assert payload["rows"][0]["symbol"] == "NIFTY"
        # blank filter matches everything
        assert build_missing_sessions_payload(rows, symbol="  ")["datasets"] == 2
        assert build_missing_sessions_payload(rows, exchange="bse")["datasets"] == 0

    def test_interval_filter_within_fetched(self) -> None:
        rows = [_row("NIFTY", "1minute", "2024-01-02", "2024-01-05", frozenset())]
        assert build_missing_sessions_payload(rows, interval="1minute")["datasets"] == 1
        assert build_missing_sessions_payload(rows, interval="5minute")["datasets"] == 0

    def test_total_missing_sums_non_abstained(self) -> None:
        rows = [
            _row("NIFTY", "1minute", "2024-01-02", "2024-01-05", frozenset()),  # 4 missing
            _row("BANKNIFTY", "1minute", None, None, frozenset()),  # abstains
        ]
        payload = build_missing_sessions_payload(rows)
        assert payload["datasets"] == 2
        assert payload["total_missing"] == 4
        # sorted by (symbol, exchange, interval) → BANKNIFTY first
        assert payload["rows"][0]["symbol"] == "BANKNIFTY"
        assert payload["rows"][0]["abstained"] is True

    def test_missing_session_dates_key_abstains(self) -> None:
        row = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
            "first_date": "2024-01-02",
            "last_date": "2024-01-05",
        }  # no "session_dates" key
        payload = build_missing_sessions_payload([row])
        assert payload["rows"][0]["abstained"] is True
        assert payload["rows"][0]["abstain_reason"] == ABSTAIN_NO_STORED_DATES

    def test_real_date_objects_coerced(self) -> None:
        # summarize_spot_datasets returns real date objects in first/last + session_dates.
        rows = [
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "first_date": date(2024, 1, 2),
                "last_date": date(2024, 1, 5),
                "session_dates": frozenset({date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)}),
            }
        ]
        payload = build_missing_sessions_payload(rows)
        assert payload["rows"][0]["missing_dates"] == ["2024-01-03"]

    def test_range_override_echoed_in_payload(self) -> None:
        rows = [_row("NIFTY", "1minute", "2024-01-22", "2024-01-26", frozenset())]
        payload = build_missing_sessions_payload(
            rows, range_start="2024-01-01", range_end="2024-01-31"
        )
        assert payload["range_start"] == "2024-01-01"
        assert payload["range_end"] == "2024-01-31"


class TestExplicitRangeOverride:
    """Review fold: a typo'd EXPLICIT override abstains — never a silent stored-span
    fall-back that fakes an interior-only audit (bad_range_override, per row)."""

    def test_junk_range_start_with_valid_stored_bound_abstains(self) -> None:
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2)}),
            range_start="2020-13-01",  # month 13 — the operator's typo
        )
        assert result.abstained
        assert result.abstain_reason == ABSTAIN_BAD_RANGE_OVERRIDE
        # The RAW junk is echoed so the operator sees exactly what was refused.
        assert result.range_start == "2020-13-01"
        assert result.expected_count is None and result.missing_dates == ()

    def test_junk_range_end_with_valid_stored_bound_abstains(self) -> None:
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2)}),
            range_end="not-a-date",
        )
        assert result.abstained
        assert result.abstain_reason == ABSTAIN_BAD_RANGE_OVERRIDE
        assert result.range_end == "not-a-date"

    def test_blank_override_is_not_provided(self) -> None:
        # A UI form's empty ?range_start= is "no override", never a refused typo.
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)}),
            range_start="  ",
            range_end="",
        )
        assert not result.abstained
        assert result.range_start == "2024-01-02"  # the stored span applied

    def test_valid_override_still_wins_over_stored(self) -> None:
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2)}),
            range_start="2024-01-01",
            range_end="2024-01-03",
        )
        assert not result.abstained
        assert result.range_start == "2024-01-01"
        assert result.range_end == "2024-01-03"

    def test_payload_rows_carry_bad_override_abstain(self) -> None:
        rows = [
            _row("NIFTY", "1minute", "2024-01-22", "2024-01-26", frozenset()),
        ]
        payload = build_missing_sessions_payload(rows, range_start="2020-13-01")
        assert payload["rows"][0]["abstained"] is True
        assert payload["rows"][0]["abstain_reason"] == ABSTAIN_BAD_RANGE_OVERRIDE
        assert payload["total_missing"] == 0  # abstained rows never fabricate a count


class TestExpectedSessionsMemo:
    """Review fold: ONE payload build pays the expected-session calendar walk once per
    DISTINCT (start, end, exchange) range — not once per dataset."""

    def test_identical_ranges_walk_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.dashboard.coverage_missing as cm

        calls: list[tuple[date, date, str | None]] = []
        real = trading_days_between

        def _spy(start: date, end: date, *, exchange: str | None = None) -> list[date]:
            calls.append((start, end, exchange))
            return real(start, end, exchange=exchange)

        monkeypatch.setattr(cm, "trading_days_between", _spy)
        stored = frozenset({date(2024, 1, 2)})
        rows = [
            _row("NIFTY", "1minute", "2024-01-02", "2024-01-05", stored),
            _row("BANKNIFTY", "1minute", "2024-01-02", "2024-01-05", stored),
            _row("RELIANCE", "1minute", "2024-01-02", "2024-01-05", stored),
        ]
        payload = build_missing_sessions_payload(rows)
        assert payload["datasets"] == 3
        # Three same-range NSE datasets → exactly ONE day-walk.
        assert calls == [(date(2024, 1, 2), date(2024, 1, 5), "NSE")]

    def test_distinct_ranges_each_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.dashboard.coverage_missing as cm

        calls: list[tuple[date, date, str | None]] = []
        real = trading_days_between

        def _spy(start: date, end: date, *, exchange: str | None = None) -> list[date]:
            calls.append((start, end, exchange))
            return real(start, end, exchange=exchange)

        monkeypatch.setattr(cm, "trading_days_between", _spy)
        stored = frozenset({date(2024, 1, 2)})
        rows = [
            _row("NIFTY", "1minute", "2024-01-02", "2024-01-05", stored),
            _row("BANKNIFTY", "1minute", "2024-01-02", "2024-01-10", stored),
        ]
        build_missing_sessions_payload(rows)
        assert len(calls) == 2

    def test_direct_call_without_cache_recomputes(self) -> None:
        # The default (every direct caller) stays cache-less — byte-identical result.
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2)}),
            expected_cache=None,
        )
        assert result.expected_count == 4

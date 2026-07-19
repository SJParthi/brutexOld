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
from brutex.dashboard import coverage_missing as cm
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
            "missing_before_first_stored",
            "missing_dates",
            "missing_truncated",
            "abstained",
            "abstain_reason",
            "range_end_clamped_to_today",
        }
        assert isinstance(d["missing_dates"], list)
        assert d["range_end_clamped_to_today"] is False  # no clamp without an injected today

    def test_pre_stored_span_split_under_floor_override(self) -> None:
        """Combined-PR fix round 2: an override before the first stored session SPLITS.

        A late-listed symbol audited against the 2020-style floor: the dates before
        its first stored session are counted in ``missing_before_first_stored`` so
        they are never conflated with interior re-pullable vendor holes. The audit
        deliberately does NOT clamp — a genuinely-missing early history stays visible.
        """
        # Stored {03-Jan, 05-Jan 2024}; range override 2024-01-01 → 2024-01-05.
        # Expected sessions: 01/02/03/04/05 (Mon–Fri, all trading days) — 01 + 02
        # precede the first stored session (pre-history), 04 is an interior hole.
        stored = frozenset({date(2024, 1, 3), date(2024, 1, 5)})
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "LATELIST",
            DRILL_INTERVAL,
            "2024-01-03",  # stored span begins here
            "2024-01-05",
            stored,
            range_start="2024-01-01",
            range_end="2024-01-05",
        )
        assert not result.abstained
        # Missing = 01-Jan + 02-Jan (pre-first-stored) + 04-Jan (interior hole).
        assert result.missing_dates == ("2024-01-01", "2024-01-02", "2024-01-04")
        assert result.missing_count == 3
        assert result.missing_before_first_stored == 2  # 01 + 02 precede 03-Jan

    def test_default_interior_audit_split_is_zero(self) -> None:
        # No override: the range starts AT the first stored session, so no missing
        # date can precede it — the split reads 0 (never None on a served audit).
        stored = frozenset({date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)})
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-02", "2024-01-05", stored
        )
        assert result.missing_count == 1
        assert result.missing_before_first_stored == 0

    def test_empty_stored_set_split_is_conservative_zero(self) -> None:
        # ZERO stored sessions ⇒ no first-stored anchor ⇒ the split is an honest 0
        # (every gap counts as ordinary missing; nothing is silently reclassified).
        result = compute_missing_sessions(
            "NSE", "INDEX", "NIFTY", DRILL_INTERVAL, "2024-01-02", "2024-01-05", frozenset()
        )
        assert result.missing_count == 4
        assert result.missing_before_first_stored == 0

    def test_abstained_split_is_none(self) -> None:
        result = compute_missing_sessions("NSE", "INDEX", "NIFTY", DRILL_INTERVAL, None, None, None)
        assert result.abstained
        assert result.missing_before_first_stored is None


class TestFutureEndClamp:
    """Continuation-v2 fix (+ v3 residue): an at/after-today audit end never fabricates gaps.

    Fails-before (v2): the only end-side guard was ``end.year >
    HOLIDAY_COVERAGE_LAST_YEAR``, so an explicit ``range_end`` later in the coverage
    year passed and every not-yet-occurred trading session counted into
    ``missing_count``/``total_missing`` — which the route, the drill caption and the
    ledger all present as re-pullable vendor holes. Fails-before (v3 residue): the v2
    clamp was strict ``end > today``, so an explicit ``range_end`` equal to (or clamped
    to) *today* kept TODAY's own session in the expected set — pre-close it has not
    occurred, post-close it is still un-pullable (``resolve_candle_range`` caps at the
    previous day), so ``missing_count`` over-counted by exactly one per dataset. The
    audit end now clamps at ``today − 1 day`` whenever ``end >= today`` (the route binds
    ``now_ist().date()``; the module stays pure) with the clamp NAMED on the verdict.
    """

    def test_future_range_end_clamped_to_today_and_named(self) -> None:
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 5)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            stored,
            range_end="2024-06-30",
            today=date(2024, 1, 10),
        )
        assert not result.abstained
        # The AUDITED (clamped) end is echoed: today − 1 day (v3 residue fix —
        # today's own session is never auditable, so the clamp lands on the
        # last auditable date, not on today itself).
        assert result.range_end == "2024-01-09"
        assert result.range_end_clamped_to_today is True
        expected = trading_days_between(date(2024, 1, 2), date(2024, 1, 9))
        assert result.expected_count == len(expected)
        # No fabricated future-or-today session — every missing date is before today.
        assert all(d < "2024-01-10" for d in result.missing_dates)

    def test_no_today_applies_no_clamp(self) -> None:
        # Pure/offline callers (today=None) keep the prior behaviour byte-identical:
        # the future range_end is audited verbatim (the pre-fix envelope).
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 5)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            stored,
            range_end="2024-06-30",
        )
        assert result.range_end == "2024-06-30"
        assert result.range_end_clamped_to_today is False
        assert any(d > "2024-01-10" for d in result.missing_dates)

    def test_past_end_is_not_clamped(self) -> None:
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 5)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            stored,
            today=date(2024, 2, 1),
        )
        assert result.range_end == "2024-01-05"
        assert result.range_end_clamped_to_today is False
        assert result.missing_count == 0

    def test_end_exactly_today_clamps_to_yesterday(self) -> None:
        # The boundary law (v3 residue fix — fails-before): end == today CLAMPS
        # to today − 1 day. Today's session is never auditable — pre-close it
        # has not occurred, post-close it is still un-pullable
        # (resolve_candle_range caps every pull at the previous day) — so it
        # must never be an expected session, let alone a re-pullable hole.
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 5)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            stored,
            today=date(2024, 1, 5),
        )
        assert result.range_end == "2024-01-04"
        assert result.range_end_clamped_to_today is True
        assert result.missing_count == 0

    def test_explicit_range_end_today_never_counts_today_as_missing(self) -> None:
        # THE v3 residue repro (fails-before): an explicit ?range_end= equal to
        # *today* (2024-01-05, a real NSE Friday session) with today NOT stored
        # counted today into missing_count — "sessions to re-pull" over-counted
        # by exactly one per dataset even though the session is un-pullable.
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 4)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-04",
            stored,
            range_end="2024-01-05",
            today=date(2024, 1, 5),
        )
        assert not result.abstained
        assert result.range_end == "2024-01-04"
        assert result.range_end_clamped_to_today is True
        assert result.missing_count == 0  # pre-fix: 1 (today fabricated as a hole)
        assert "2024-01-05" not in result.missing_dates

    def test_fully_future_range_abstains_with_clamp_named(self) -> None:
        # Both bounds after today: the clamp collapses the span (end < start) →
        # the honest NO_RANGE abstain still NAMES the clamp (never a fabricated
        # future-only gap list, never a silent clamp).
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            frozenset({date(2024, 1, 2)}),
            range_start="2024-02-01",
            range_end="2024-03-01",
            today=date(2024, 1, 10),
        )
        assert result.abstained
        assert result.abstain_reason == ABSTAIN_NO_RANGE
        assert result.range_end == "2024-01-09"
        assert result.range_end_clamped_to_today is True

    def test_out_of_coverage_future_year_clamps_then_serves(self) -> None:
        # The clamp runs BEFORE the coverage-year check, so a far-future
        # range_end audits honestly up to today instead of abstaining
        # out_of_coverage (the served-vs-abstain ordering pin).
        high_year = HOLIDAY_COVERAGE_LAST_YEAR + 1
        stored = frozenset(trading_days_between(date(2024, 1, 2), date(2024, 1, 5)))
        result = compute_missing_sessions(
            "NSE",
            "INDEX",
            "NIFTY",
            DRILL_INTERVAL,
            "2024-01-02",
            "2024-01-05",
            stored,
            range_end=f"{high_year}-01-05",
            today=date(2024, 1, 10),
        )
        assert not result.abstained
        assert result.range_end == "2024-01-09"
        assert result.range_end_clamped_to_today is True

    def test_payload_threads_today_to_every_row(self) -> None:
        rows = [
            _row("NIFTY", DRILL_INTERVAL, "2024-01-02", "2024-01-05", [date(2024, 1, 2)]),
        ]
        payload = build_missing_sessions_payload(
            rows, range_end="2024-06-30", today=date(2024, 1, 10)
        )
        assert payload["datasets"] == 1
        row = payload["rows"][0]
        assert row["range_end_clamped_to_today"] is True
        assert row["range_end"] == "2024-01-09"
        assert all(d < "2024-01-10" for d in row["missing_dates"])


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
        # No override → nothing precedes the (absent) first-stored anchor.
        assert payload["total_missing_before_first_stored"] == 0
        # sorted by (symbol, exchange, interval) → BANKNIFTY first
        assert payload["rows"][0]["symbol"] == "BANKNIFTY"
        assert payload["rows"][0]["abstained"] is True

    def test_total_missing_before_first_stored_sums_split(self) -> None:
        """Combined-PR fix round 2: the payload headline carries the pre-stored split."""
        rows = [
            # Stored {03, 05} — under the 01→05 override: missing 01+02 (pre) + 04 (interior).
            _row(
                "NIFTY",
                "1minute",
                "2024-01-03",
                "2024-01-05",
                frozenset({date(2024, 1, 3), date(2024, 1, 5)}),
            ),
        ]
        payload = build_missing_sessions_payload(
            rows, range_start="2024-01-01", range_end="2024-01-05"
        )
        assert payload["total_missing"] == 3
        assert payload["total_missing_before_first_stored"] == 2
        assert payload["rows"][0]["missing_before_first_stored"] == 2

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

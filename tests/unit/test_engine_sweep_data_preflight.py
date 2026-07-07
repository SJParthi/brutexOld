"""Tests for `brutex.engine.sweep_data_preflight` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).

WP2 unit suite — the pure preflight cores: RefusalCode vocabulary, the
deterministic date clamp (BUILD-SPEC §2.1/§4; extreme tests 1-3), the
refuse-whole disk-coverage fold (extreme tests 4-5 + the row-18 fold twin),
the bimodal ledger-quarantine fold (extreme test 7), and the exclusion
report. Test names carry the binding-law R-numbers; docstrings cite the law.
"""

from __future__ import annotations

# Whitebox suite: drift-pins the module-private restated constants + the span
# walker (house precedent — DEC-P0-017 file-top narrow suppression).
# pyright: reportPrivateUsage=none
from datetime import date
from itertools import pairwise

from brutex.data.session_coverage import (
    DATE_CHECKED_INTERVALS,
    LedgerRowFlag,
    PairScan,
    QuarantineScan,
    ScanKind,
)
from brutex.data.window_ledger import (
    DEMOTED_SETTLE_REASONS,
    FAILED,
    SKIPPED,
    expired_window_settle_reason,
)
from brutex.engine.sweep_data_preflight import (
    _DATE_CHECKED_INTERVALS,
    MAX_SPANS_NAMED,
    SWEEP_HISTORY_FLOOR,
    CoverageReport,
    DateClampReport,
    ExclusionReport,
    LedgerFlag,
    MissingSpan,
    PairVerdict,
    QuarantineReport,
    RefusalCode,
    _missing_spans,
    clamp_dates,
    preflight_disk_coverage,
    preflight_quarantine,
)

#: A mid-2026 Wednesday (verified 2026-06-17 = Wednesday) — the injected clock.
TODAY = date(2026, 6, 17)

INTERIOR_GAP = "INTERIOR_GAP"
ALL_FILTERED = "ALL_FILTERED"


def _dates_scan(
    symbol: str,
    interval: str,
    covered: frozenset[date],
    corrupt: tuple[str, ...] = (),
) -> PairScan:
    """Build a DATES-kind PairScan test double."""
    return PairScan(
        symbol=symbol,
        interval=interval,
        kind=ScanKind.DATES,
        covered=covered,
        present=bool(covered),
        corrupt_files=corrupt,
        bars_found=len(covered),
        months_probed=1,
    )


def _presence_scan(
    symbol: str,
    interval: str,
    *,
    present: bool,
    corrupt: tuple[str, ...] = (),
) -> PairScan:
    """Build a PRESENCE-kind PairScan test double."""
    return PairScan(
        symbol=symbol,
        interval=interval,
        kind=ScanKind.PRESENCE,
        covered=frozenset(),
        present=present,
        corrupt_files=corrupt,
        bars_found=0,
        months_probed=1,
    )


def _flag(
    status: str,
    last_error: str,
    *,
    security_id: str = "NSE-NIFTY",
    interval: str = "1minute",
    window_start: str = "2023-05-01",
    window_end: str = "2023-05-31",
) -> LedgerRowFlag:
    """Build a LedgerRowFlag test double."""
    return LedgerRowFlag(
        security_id=security_id,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
        status=status,
        last_error=last_error,
    )


class TestRefusalCodeVocabulary:
    """The machine-stable P2-P6 contract — values never renamed/renumbered."""

    def test_all_twelve_codes_machine_stable(self) -> None:
        """Pin the exact 12 (name, value) pairs of the refusal vocabulary."""
        assert [(c.name, c.value) for c in RefusalCode] == [
            ("ZERO_SESSIONS", "zero_sessions"),
            ("REVERSED_RANGE", "reversed_range"),
            ("CALENDAR_COVERAGE_EXHAUSTED", "calendar_coverage_exhausted"),
            ("COVERAGE_MISSING", "coverage_missing"),
            ("DERIVED_TF_MISSING", "derived_tf_missing"),
            ("CORRUPT_PARTITION", "corrupt_partition"),
            ("QUARANTINED_RANGE", "quarantined_range"),
            ("LEDGER_UNREADABLE", "ledger_unreadable"),
            ("PULL_IN_FLIGHT", "pull_in_flight"),
            ("DERIVE_IN_FLIGHT", "derive_in_flight"),
            ("DISK_SPACE", "disk_space"),
            ("SLA_OVERRUN", "sla_overrun"),
        ]


class TestDriftPins:
    """Restated constants stay byte-equal to their sources (derive_lock convention)."""

    def test_sweep_history_floor_matches_pull_controller(self) -> None:
        """SWEEP_HISTORY_FLOOR == pull_controller.GROWW_HISTORY_FLOOR (drift pin).

        The engine must not import brutex.dashboard (layering law) — the TEST
        imports both sides and pins byte equality.
        """
        from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR

        assert SWEEP_HISTORY_FLOOR == GROWW_HISTORY_FLOOR

    def test_date_checked_intervals_match_scanner(self) -> None:
        """The restated DATES-interval set equals the scanner's (drift pin)."""
        assert _DATE_CHECKED_INTERVALS == DATE_CHECKED_INTERVALS


class TestClampFloor:
    """Extreme test 1 (R4) — the pre-2020 floor clamp with a visible note."""

    def test_r4_pre_2020_start_clamps_to_floor_with_visible_note(self) -> None:
        """R4: a 2019-06-01 start clamps to 2020-01-01 with the pinned note."""
        report = clamp_dates(date(2019, 6, 1), date(2020, 3, 31), today=TODAY)
        assert report.start == date(2020, 1, 1)
        assert report.requested_start == date(2019, 6, 1)
        assert report.notes == (
            "start clamped 2019-06-01 → 2020-01-01 (requested start precedes "
            "SWEEP_HISTORY_FLOOR 2020-01-01, the Track-1 data pull floor)",
        )
        assert report.ok is True
        assert report.expected_sessions  # a real Q1-2020 session list

    def test_r4_start_on_or_after_floor_not_moved_no_note(self) -> None:
        """R4: an in-floor start is untouched and produces NO note."""
        report = clamp_dates(date(2023, 5, 2), date(2023, 5, 5), today=TODAY)
        assert report.start == date(2023, 5, 2)
        assert report.notes == ()
        assert report.refusal() == ""
        assert report.refusal_codes == ()


class TestClampPullDeliverableEnd:
    """Extreme test 2 (R4) — the pull-deliverable end bound (today − 1, session-rolled)."""

    def test_r4_future_end_clamps_to_last_pull_deliverable_session(self) -> None:
        """R4: end=2099-01-01 with today=Wed 2026-06-17 clamps to Tue 2026-06-16."""
        report = clamp_dates(date(2026, 6, 1), date(2099, 1, 1), today=TODAY)
        assert report.end == date(2026, 6, 16)
        assert report.end <= TODAY - date.resolution  # never today
        assert report.notes == (
            "end clamped 2099-01-01 → 2026-06-16 (last pull-deliverable session "
            "on/before today − 1 — the default /universe pull's upper bound is "
            "the previous day)",
        )
        assert report.ok is True

    def test_r4_weekend_rollback_arm(self) -> None:
        """R4: today=Mon 2026-06-15 ⇒ today−1 is Sunday ⇒ rolls back to Fri 2026-06-12."""
        report = clamp_dates(date(2026, 6, 1), date(2099, 1, 1), today=date(2026, 6, 15))
        assert report.end == date(2026, 6, 12)

    def test_r4_holiday_rollback_arm(self) -> None:
        """R4: today=Tue 2026-01-27 ⇒ today−1 is Republic Day ⇒ rolls to Fri 2026-01-23."""
        report = clamp_dates(date(2026, 1, 2), date(2099, 1, 1), today=date(2026, 1, 27))
        assert report.end == date(2026, 1, 23)

    def test_r4_observed_overlay_rollback_arm(self) -> None:
        """R4: an observed-overlay today−1 rolls the end bound back past it."""
        report = clamp_dates(
            date(2026, 6, 1),
            date(2099, 1, 1),
            today=TODAY,
            observed=frozenset({date(2026, 6, 16)}),
        )
        assert report.end == date(2026, 6, 15)  # Monday
        assert date(2026, 6, 16) not in report.expected_sessions

    def test_r4_mid_session_default_range_never_expects_today(self) -> None:
        """R4: a mid-session launch of the default 2020→∞ range does NOT expect today.

        The forming-session + default-pull-gap false-red is dead: the clamped
        end is the last PULL-DELIVERABLE session, so *today* is never in the
        expected list.
        """
        report = clamp_dates(date(2020, 1, 1), date(2099, 1, 1), today=TODAY)
        assert report.end == date(2026, 6, 16)
        assert TODAY not in report.expected_sessions
        assert report.expected_sessions[0] == date(2020, 1, 1)
        assert report.expected_sessions[-1] == date(2026, 6, 16)

    def test_r4_past_end_on_non_session_date_not_moved(self) -> None:
        """R4: a past requested_end on a Saturday is NOT moved (enumeration handles it)."""
        report = clamp_dates(date(2023, 5, 2), date(2023, 5, 6), today=TODAY)
        assert report.end == date(2023, 5, 6)
        assert report.notes == ()
        assert report.expected_sessions[-1] == date(2023, 5, 5)

    def test_r4_both_endpoints_clamped_two_notes(self) -> None:
        """R4: both endpoints moved ⇒ both visible notes, in clamp order."""
        report = clamp_dates(date(2019, 1, 1), date(2099, 1, 1), today=TODAY)
        assert len(report.notes) == 2
        assert report.notes[0].startswith("start clamped 2019-01-01 → 2020-01-01")
        assert report.notes[1].startswith("end clamped 2099-01-01 → 2026-06-16")


class TestClampRefusals:
    """Extreme test 3 (R4) + the reversed / calendar-horizon gates."""

    def test_r4_zero_session_range_refuses_honestly(self) -> None:
        """R4: a Sat→Sun range holds 0 expected sessions and refuses honestly."""
        report = clamp_dates(date(2023, 5, 6), date(2023, 5, 7), today=TODAY)
        assert report.zero_sessions is True
        assert report.ok is False
        assert report.expected_sessions == ()
        assert report.refusal_codes == (RefusalCode.ZERO_SESSIONS,)
        assert "0 expected sessions" in report.refusal()
        assert "widen the range" in report.refusal()

    def test_r4_all_holiday_span_refuses_zero_sessions(self) -> None:
        """R4: Sat + Sun + Republic-Day Monday ⇒ 0 expected sessions ⇒ refuse."""
        report = clamp_dates(date(2026, 1, 24), date(2026, 1, 26), today=TODAY)
        assert report.zero_sessions is True
        assert "0 expected sessions" in report.refusal()

    def test_r4_reversed_range_refuses(self) -> None:
        """R4: a genuinely reversed request refuses REVERSED_RANGE, never sweeps empty."""
        report = clamp_dates(date(2023, 6, 1), date(2023, 5, 1), today=TODAY)
        assert report.reversed_range is True
        assert report.ok is False
        assert report.zero_sessions is False  # the flag names its TRUE cause
        assert report.expected_sessions == ()
        assert report.refusal_codes == (RefusalCode.REVERSED_RANGE,)
        assert "reversed range after clamping" in report.refusal()
        assert "fix the requested dates" in report.refusal()

    def test_r4_fully_future_range_clamps_past_start_and_refuses_reversed(self) -> None:
        """R4: a fully-future range clamps its end past its start ⇒ REVERSED_RANGE."""
        report = clamp_dates(date(2099, 1, 1), date(2099, 12, 31), today=TODAY)
        assert report.start == date(2099, 1, 1)
        assert report.end == date(2026, 6, 16)
        assert report.reversed_range is True
        assert RefusalCode.REVERSED_RANGE in report.refusal_codes

    def test_r4_out_of_calendar_refuses_with_horizon_remedy(self) -> None:
        """D4: a clamped 2027 end refuses CALENDAR_COVERAGE_EXHAUSTED, no fabricated sessions."""
        report = clamp_dates(date(2020, 1, 1), date(2027, 6, 1), today=date(2027, 6, 16))
        assert report.out_of_calendar is True
        assert report.ok is False
        assert report.expected_sessions == ()  # the unguarded enumerator was never consulted
        assert report.zero_sessions is False
        assert report.refusal_codes == (RefusalCode.CALENDAR_COVERAGE_EXHAUSTED,)
        assert "2020..2026" in report.refusal()
        assert "land HOLIDAYS_2027 in brutex/calendars/nse.py" in report.refusal()
        assert "before sweeping 2027 dates" in report.refusal()

    def test_r4_reversed_and_out_of_calendar_gather_both_codes(self) -> None:
        """Gather-all: a reversed AND out-of-horizon range names BOTH causes."""
        report = clamp_dates(date(2028, 1, 3), date(2027, 6, 1), today=date(2027, 6, 16))
        assert report.reversed_range is True
        assert report.out_of_calendar is True
        assert report.refusal_codes == (
            RefusalCode.REVERSED_RANGE,
            RefusalCode.CALENDAR_COVERAGE_EXHAUSTED,
        )
        text = report.refusal()
        assert "reversed range after clamping" in text
        assert "land HOLIDAYS_2027" in text


class TestClampCalendarSides:
    """Extreme test 5 (R4) — Muhurat excluded, Budget Sunday expected, overlay subtracted."""

    def test_r4_muhurat_day_never_expected(self) -> None:
        """R4: the 2025-10-21 Muhurat date is EXCLUDED from the expected list."""
        report = clamp_dates(date(2025, 10, 17), date(2025, 10, 23), today=TODAY)
        assert report.expected_sessions == (
            date(2025, 10, 17),
            date(2025, 10, 20),
            date(2025, 10, 23),
        )
        assert date(2025, 10, 21) not in report.expected_sessions

    def test_r4_muhurat_special_weekend_never_counted_missing(self) -> None:
        """R4: stored Muhurat bars read as never-missing extras; absent Budget Sunday flags.

        Extreme test 5: a lake holding the 2025-10-21 Muhurat 60-bar extras is
        never flagged (the day is not expected); the 2026-02-01 Budget SUNDAY
        IS expected and counts missing when absent on disk.
        """
        muhurat = clamp_dates(date(2025, 10, 17), date(2025, 10, 23), today=TODAY)
        covered = frozenset(muhurat.expected_sessions) | {date(2025, 10, 21)}
        scans = {("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", covered)}
        report = preflight_disk_coverage(
            muhurat.expected_sessions, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert report.ok is True

        budget = clamp_dates(date(2026, 1, 30), date(2026, 2, 3), today=TODAY)
        assert date(2026, 2, 1) in budget.expected_sessions  # the Budget SUNDAY is expected
        without_sunday = frozenset(budget.expected_sessions) - {date(2026, 2, 1)}
        scans = {("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", without_sunday)}
        report = preflight_disk_coverage(
            budget.expected_sessions, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert report.ok is False
        (verdict,) = report.not_ready
        assert verdict.reason is RefusalCode.COVERAGE_MISSING
        assert verdict.spans == (
            MissingSpan(start=date(2026, 2, 1), end=date(2026, 2, 1), sessions=1),
        )
        assert "2026-02-01 (1 session)" in verdict.describe()

    def test_r4_observed_overlay_day_subtracted_from_expected(self) -> None:
        """R4: an observed-overlay day is not expected — its absence on disk never flags."""
        report = clamp_dates(
            date(2023, 5, 2),
            date(2023, 5, 5),
            today=TODAY,
            observed=frozenset({date(2023, 5, 3)}),
        )
        assert report.expected_sessions == (
            date(2023, 5, 2),
            date(2023, 5, 4),
            date(2023, 5, 5),
        )
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan(
                "NSE-NIFTY", "1minute", frozenset(report.expected_sessions)
            )
        }
        coverage = preflight_disk_coverage(
            report.expected_sessions, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert coverage.ok is True


class TestMissingSpans:
    """The MissingSpan walker — contiguity in the EXPECTED sequence."""

    def test_holiday_between_missing_sessions_does_not_split_span(self) -> None:
        """Two missing sessions separated by a weekend + holiday form ONE span."""
        expected = clamp_dates(date(2023, 4, 28), date(2023, 5, 2), today=TODAY)
        assert expected.expected_sessions == (date(2023, 4, 28), date(2023, 5, 2))
        spans = _missing_spans(expected.expected_sessions, frozenset())
        assert spans == (MissingSpan(start=date(2023, 4, 28), end=date(2023, 5, 2), sessions=2),)

    def test_spans_partition_missing_exactly(self) -> None:
        """Spans are disjoint, ordered, and their sessions sum to the missing total."""
        expected = tuple(date(2024, 1, 1) + n * date.resolution for n in range(30))
        covered = frozenset(d for i, d in enumerate(expected) if i % 3 == 0)
        spans = _missing_spans(expected, covered)
        assert sum(s.sessions for s in spans) == len(expected) - len(covered)
        for earlier, later in pairwise(spans):
            assert earlier.end < later.start
        for span in spans:
            assert span.start <= span.end

    def test_trailing_missing_run_is_flushed(self) -> None:
        """A run of missing sessions reaching the end of the range is emitted."""
        expected = (date(2023, 5, 2), date(2023, 5, 3), date(2023, 5, 4))
        spans = _missing_spans(expected, frozenset({date(2023, 5, 2)}))
        assert spans == (MissingSpan(start=date(2023, 5, 3), end=date(2023, 5, 4), sessions=2),)

    def test_fully_covered_yields_no_spans(self) -> None:
        """A fully covered expected list yields zero spans."""
        expected = (date(2023, 5, 2), date(2023, 5, 3))
        assert _missing_spans(expected, frozenset(expected)) == ()

    def test_describe_single_and_multi_day_forms(self) -> None:
        """Span rendering: one date for a single day, start→end (N sessions) otherwise."""
        single = MissingSpan(start=date(2023, 5, 2), end=date(2023, 5, 2), sessions=1)
        multi = MissingSpan(start=date(2023, 5, 2), end=date(2023, 5, 4), sessions=3)
        assert single.describe() == "2023-05-02 (1 session)"
        assert multi.describe() == "2023-05-02→2023-05-04 (3 sessions)"


class TestCoverageFoldRefuseWhole:
    """Extreme test 4 (R4+O-a) — refuse-whole naming ALL pairs + exact spans."""

    def test_r4_oa_partial_availability_refuses_whole_naming_all_pairs_and_spans(self) -> None:
        """R4+O-a: one refusal names the COMPLETE not-ready list + exact spans + remedy.

        2 symbols × 2 TFs; one pair missing 3 sessions AND a second broken
        pair (a never-materialized derived TF) — BOTH are named in the SAME
        text (gather-all); the READY pairs are not named not-ready.
        """
        clamp = clamp_dates(date(2023, 4, 24), date(2023, 5, 8), today=TODAY)
        expected = clamp.expected_sessions
        assert len(expected) == 10
        full = frozenset(expected)
        hole = full - {date(2023, 5, 2), date(2023, 5, 3), date(2023, 5, 4)}
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", full),
            ("NSE-NIFTY", "5minute"): _presence_scan("NSE-NIFTY", "5minute", present=True),
            ("NSE-BANKNIFTY", "1minute"): _dates_scan("NSE-BANKNIFTY", "1minute", hole),
            ("NSE-BANKNIFTY", "5minute"): _presence_scan("NSE-BANKNIFTY", "5minute", present=False),
        }
        report = preflight_disk_coverage(
            expected,
            scans,
            symbols=["NSE-NIFTY", "NSE-BANKNIFTY"],
            intervals=["1minute", "5minute"],
        )
        assert report.ok is False
        assert report.checked_pairs == 4
        assert report.expected_sessions == 10
        assert [(v.symbol, v.interval, v.reason) for v in report.not_ready] == [
            ("NSE-BANKNIFTY", "1minute", RefusalCode.COVERAGE_MISSING),
            ("NSE-BANKNIFTY", "5minute", RefusalCode.DERIVED_TF_MISSING),
        ]
        text = report.refusal()
        assert "NSE-BANKNIFTY × 1minute" in text
        assert "2023-05-02→2023-05-04 (3 sessions)" in text
        assert "NSE-BANKNIFTY × 5minute" in text  # the second broken pair, SAME text
        assert "never materialized" in text
        assert "pull on /universe first" in text
        assert "the sweep NEVER pulls (R3)" in text
        assert "NSE-NIFTY × 1minute" not in text  # ready pairs are not named not-ready
        assert report.covered_sessions[("NSE-NIFTY", "1minute")] == 10
        assert report.covered_sessions[("NSE-BANKNIFTY", "1minute")] == 7

    def test_ok_report_has_empty_refusal_and_remediation_constant(self) -> None:
        """A fully-ready selection is ok; refusal() is empty; remediation names R3."""
        expected = (date(2023, 5, 2), date(2023, 5, 3))
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", frozenset(expected)),
        }
        report = preflight_disk_coverage(
            expected, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert report.ok is True
        assert report.refusal() == ""
        assert report.remediation() == (
            "pull on /universe first (▶ Pull · full pipeline); the sweep NEVER pulls (R3)"
        )

    def test_absent_dates_scan_key_is_not_ready_fail_closed(self) -> None:
        """Fail-closed: an ABSENT scan key for a DATES pair reads as zero coverage."""
        expected = (date(2023, 5, 2), date(2023, 5, 3), date(2023, 5, 4))
        report = preflight_disk_coverage(expected, {}, symbols=["NSE-NIFTY"], intervals=["1minute"])
        assert report.ok is False
        (verdict,) = report.not_ready
        assert verdict.reason is RefusalCode.COVERAGE_MISSING
        assert verdict.spans == (
            MissingSpan(start=date(2023, 5, 2), end=date(2023, 5, 4), sessions=3),
        )
        assert verdict.detail == "3 of 3 expected sessions absent on disk"
        assert report.covered_sessions[("NSE-NIFTY", "1minute")] == 0

    def test_absent_presence_scan_key_is_derived_tf_missing(self) -> None:
        """Fail-closed: an ABSENT scan key for a derived TF is DERIVED_TF_MISSING."""
        expected = (date(2023, 5, 2),)
        report = preflight_disk_coverage(expected, {}, symbols=["NSE-NIFTY"], intervals=["5minute"])
        assert report.ok is False
        (verdict,) = report.not_ready
        assert verdict.reason is RefusalCode.DERIVED_TF_MISSING
        assert verdict.spans == ()
        assert verdict.detail == "derived TF never materialized in the requested span"

    def test_empty_expected_and_empty_selection_trivially_ok(self) -> None:
        """Degenerate inputs are trivially ok (the clamp gate refuses upstream)."""
        empty = preflight_disk_coverage((), {}, symbols=[], intervals=[])
        assert empty.ok is True
        assert empty.checked_pairs == 0
        assert empty.expected_sessions == 0
        no_sessions = preflight_disk_coverage(
            (),
            {("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", frozenset())},
            symbols=["NSE-NIFTY"],
            intervals=["1minute"],
        )
        assert no_sessions.ok is True


class TestCoverageFoldCorrupt:
    """Extreme test 18 fold twin (R38-class) — corrupt files are NAMED refusals."""

    def test_r38_corrupt_partition_fold_names_path_not_traceback(self) -> None:
        """R38-class: a corrupt file folds to CORRUPT_PARTITION carrying the path."""
        expected = (date(2023, 5, 2),)
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan(
                "NSE-NIFTY",
                "1minute",
                frozenset(expected),
                corrupt=("/lake/bars/NSE/INDEX/NSE-NIFTY/1minute/2023/05.parquet",),
            )
        }
        report = preflight_disk_coverage(
            expected, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert report.ok is False
        (verdict,) = report.not_ready  # dates all covered — corruption ALONE is not-ready
        assert verdict.reason is RefusalCode.CORRUPT_PARTITION
        assert verdict.spans == ()
        assert "/lake/bars/NSE/INDEX/NSE-NIFTY/1minute/2023/05.parquet" in verdict.detail
        assert "/lake/bars/NSE/INDEX/NSE-NIFTY/1minute/2023/05.parquet" in report.refusal()

    def test_corrupt_and_missing_both_gathered_for_one_pair(self) -> None:
        """Gather-all: a pair both corrupt AND missing dates carries BOTH verdicts."""
        expected = (date(2023, 5, 2), date(2023, 5, 3))
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan(
                "NSE-NIFTY", "1minute", frozenset(), corrupt=("/lake/x.parquet",)
            )
        }
        report = preflight_disk_coverage(
            expected, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        assert [v.reason for v in report.not_ready] == [
            RefusalCode.COVERAGE_MISSING,
            RefusalCode.CORRUPT_PARTITION,
        ]

    def test_corrupt_presence_pair_is_gathered(self) -> None:
        """Defensive: a PRESENCE pair reporting corrupt files still folds the refusal."""
        scans = {
            ("NSE-NIFTY", "5minute"): _presence_scan(
                "NSE-NIFTY", "5minute", present=True, corrupt=("/lake/y.parquet",)
            )
        }
        report = preflight_disk_coverage(
            (date(2023, 5, 2),), scans, symbols=["NSE-NIFTY"], intervals=["5minute"]
        )
        (verdict,) = report.not_ready
        assert verdict.reason is RefusalCode.CORRUPT_PARTITION
        assert "/lake/y.parquet" in verdict.detail


class TestSpanCapRendering:
    """The D3 graft — spans capped at MAX_SPANS_NAMED with the TRUE totals."""

    def test_span_cap_names_true_totals(self) -> None:
        """More than MAX_SPANS_NAMED spans render '(+K more spans)' + the TRUE totals."""
        expected = tuple(date(2024, 1, 1) + n * date.resolution for n in range(30))
        covered = frozenset(d for i, d in enumerate(expected) if i % 2 == 1)
        scans = {
            ("NSE-NIFTY", "1minute"): _dates_scan("NSE-NIFTY", "1minute", covered),
        }
        report = preflight_disk_coverage(
            expected, scans, symbols=["NSE-NIFTY"], intervals=["1minute"]
        )
        (verdict,) = report.not_ready
        assert len(verdict.spans) == 15
        text = verdict.describe()
        assert text.count("(1 session)") == MAX_SPANS_NAMED
        assert "(+3 more spans; 15 spans / 15 missing sessions in total)" in text

    def test_at_or_under_cap_renders_every_span_without_tail(self) -> None:
        """At most MAX_SPANS_NAMED spans render fully — no '(+K more spans)' tail."""
        verdict = PairVerdict(
            symbol="NSE-NIFTY",
            interval="1minute",
            reason=RefusalCode.COVERAGE_MISSING,
            spans=(MissingSpan(start=date(2023, 5, 2), end=date(2023, 5, 4), sessions=3),),
            detail="3 of 10 expected sessions absent on disk",
        )
        text = verdict.describe()
        assert "2023-05-02→2023-05-04 (3 sessions)" in text
        assert "more spans" not in text

    def test_spanless_verdict_describe_is_head_only(self) -> None:
        """A presence/corrupt verdict (no spans) renders its head clause only."""
        verdict = PairVerdict(
            symbol="NSE-NIFTY",
            interval="5minute",
            reason=RefusalCode.DERIVED_TF_MISSING,
            spans=(),
            detail="derived TF never materialized in the requested span",
        )
        assert verdict.describe() == (
            "NSE-NIFTY × 5minute: derived TF never materialized in the requested span"
        )


class TestQuarantineFold:
    """Extreme test 7 (R4) — the bimodal ledger-quarantine fold, fail-CLOSED ledger."""

    def test_r4_quarantined_window_in_range_refuses(self) -> None:
        """R4: a FAILED + INTERIOR_GAP overlap refuses QUARANTINED_RANGE-class."""
        assert INTERIOR_GAP in DEMOTED_SETTLE_REASONS
        scan = QuarantineScan(flags=(_flag(FAILED, INTERIOR_GAP),), load_failed=False)
        report = preflight_quarantine(scan)
        assert report.ok is False
        assert len(report.demoted_open) == 1
        assert report.accepted_gaps == ()
        assert report.demoted_open[0] == LedgerFlag(
            security_id="NSE-NIFTY",
            interval="1minute",
            window_start="2023-05-01",
            window_end="2023-05-31",
            status=FAILED,
            last_error=INTERIOR_GAP,
        )
        assert "will re-pull" in report.remediation()
        assert "pull on /universe first" in report.remediation()

    def test_r4_skipped_bimodal_split(self) -> None:
        """R4: SKIPPED+ALL_FILTERED (accept-gap) refuses; an expired-empty settle never.

        The bimodal-SKIPPED honesty split: an accept-gap settle means the
        stored bars are KNOWN-incomplete; an expired-window "vendor confirmed
        no trades" settle is a clean no-data answer — disk stays the
        authority.
        """
        assert ALL_FILTERED in DEMOTED_SETTLE_REASONS
        accept_gap = QuarantineScan(flags=(_flag(SKIPPED, ALL_FILTERED),), load_failed=False)
        report = preflight_quarantine(accept_gap)
        assert report.ok is False
        assert report.demoted_open == ()
        assert len(report.accepted_gaps) == 1
        remedy = report.remediation()
        assert "KNOWN-incomplete" in remedy
        assert "brutex reopen-skipped" in remedy
        assert "pull on /universe first" in remedy

        expired = QuarantineScan(
            flags=(_flag(SKIPPED, expired_window_settle_reason(1)),), load_failed=False
        )
        report = preflight_quarantine(expired)
        assert report.ok is True
        assert report.accepted_gaps == ()
        assert report.remediation() == ""

    def test_r4_ledger_unreadable_fail_closed(self) -> None:
        """R4: load_failed=True refuses fail-CLOSED even with zero flags."""
        report = preflight_quarantine(QuarantineScan(flags=(), load_failed=True))
        assert report.ok is False
        assert report.ledger_load_failed is True
        assert "UNREADABLE" in report.remediation()
        assert "window_ledger/ledger.parquet" in report.remediation()

    def test_unexpected_status_never_silently_refuses(self) -> None:
        """Defensive: a non-FAILED/SKIPPED status with a demotion tag lands nowhere."""
        scan = QuarantineScan(flags=(_flag("SUCCESS", INTERIOR_GAP),), load_failed=False)
        report = preflight_quarantine(scan)
        assert report.ok is True
        assert report.demoted_open == ()
        assert report.accepted_gaps == ()

    def test_empty_scan_trivially_ok(self) -> None:
        """A clean ledger scan is trivially ok with an empty remediation."""
        report = preflight_quarantine(QuarantineScan(flags=(), load_failed=False))
        assert report.ok is True
        assert report.remediation() == ""

    def test_all_three_classes_gathered_in_one_remediation(self) -> None:
        """Gather-all: unreadable + open demotion + accepted gap all named at once."""
        scan = QuarantineScan(
            flags=(_flag(FAILED, INTERIOR_GAP), _flag(SKIPPED, ALL_FILTERED)),
            load_failed=True,
        )
        report = preflight_quarantine(scan)
        remedy = report.remediation()
        assert "UNREADABLE" in remedy
        assert "open demotions" in remedy
        assert "accepted gaps" in remedy


class TestExclusionReport:
    """The sweep-vs-pull exclusion verdict — lock wins."""

    def test_ok_when_nothing_active(self) -> None:
        """No pull, no prebuild ⇒ ok with an empty refusal."""
        report = ExclusionReport(pull_active=False, derive_active=False)
        assert report.ok is True
        assert report.refusal() == ""

    def test_pull_active_refuses_lock_wins(self) -> None:
        """A live pull refuses the sweep — lock wins, watch /universe."""
        report = ExclusionReport(pull_active=True, derive_active=False)
        assert report.ok is False
        assert report.refusal() == (
            "a pull is running for this lake — lock wins; wait for it to finish "
            "(watch /universe), then re-run the sweep"
        )

    def test_derive_active_refuses(self) -> None:
        """A live prebuild/derive refuses the sweep."""
        report = ExclusionReport(pull_active=False, derive_active=True)
        assert report.ok is False
        assert report.refusal().startswith("a prebuild is running for this lake")

    def test_both_active_named_together(self) -> None:
        """Both holders active are named together in one refusal."""
        report = ExclusionReport(pull_active=True, derive_active=True)
        assert report.refusal().startswith("a pull and a prebuild are running for this lake")
        assert "lock wins" in report.refusal()


class TestReportShapes:
    """Frozen report ergonomics the composed gate (WP5) relies on."""

    def test_clamp_report_is_frozen(self) -> None:
        """DateClampReport is immutable (frozen slots dataclass)."""
        report = clamp_dates(date(2023, 5, 2), date(2023, 5, 5), today=TODAY)
        try:
            report.start = date(2024, 1, 1)  # type: ignore[misc]
        except AttributeError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("DateClampReport must be frozen")
        assert isinstance(report, DateClampReport)

    def test_coverage_report_shape(self) -> None:
        """CoverageReport carries the observability fields the payload renders."""
        report = CoverageReport(
            not_ready=(),
            checked_pairs=2,
            expected_sessions=5,
            covered_sessions={("NSE-NIFTY", "1minute"): 5},
        )
        assert report.ok is True
        assert report.covered_sessions[("NSE-NIFTY", "1minute")] == 5

    def test_quarantine_report_ok_axes(self) -> None:
        """Each QuarantineReport axis independently flips ok."""
        base = QuarantineReport(demoted_open=(), accepted_gaps=(), ledger_load_failed=False)
        assert base.ok is True
        flag = LedgerFlag(
            security_id="NSE-NIFTY",
            interval="1minute",
            window_start="2023-05-01",
            window_end="2023-05-31",
            status=FAILED,
            last_error=INTERIOR_GAP,
        )
        assert (
            QuarantineReport(demoted_open=(flag,), accepted_gaps=(), ledger_load_failed=False).ok
            is False
        )

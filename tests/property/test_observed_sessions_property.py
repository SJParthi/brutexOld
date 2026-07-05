"""Property tests for the observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001).

Two invariants over randomized inputs:

* the pure classifier's flags are a SUBSET of the candidates, capped at
  ``MAX_FLAGS_PER_MONTH``, never carry a FAILED veto, and an over-cap verdict
  flags NOTHING;
* the consumption primitive is REMOVE-ONLY — for every date and every overlay,
  ``is_trading_day_observed`` implies ``is_trading_day`` (no input can conjure a
  session).
"""

from __future__ import annotations

from datetime import date, timedelta

from brutex.calendars.nse import is_trading_day
from brutex.calendars.observed_sessions import (
    MAX_FLAGS_PER_MONTH,
    MAX_OVERLAY_DAYS_PER_YEAR,
    DayEvidence,
    classify_month_drift,
    is_trading_day_observed,
)
from hypothesis import given
from hypothesis import strategies as st

_MONTH = date(2026, 5, 1)

_evidence_row = st.builds(
    DayEvidence,
    day=st.dates(min_value=date(2026, 5, 1), max_value=date(2026, 5, 31)),
    zero_bars_all_indices=st.booleans(),
    indices_with_month_data=st.integers(min_value=0, max_value=3),
    clean_success_windows=st.integers(min_value=0, max_value=4),
    failed_windows_touching=st.integers(min_value=0, max_value=3),
)


@given(
    evidence=st.lists(_evidence_row, max_size=12, unique_by=lambda row: row.day),
    existing_year=st.integers(min_value=0, max_value=12),
)
def test_flags_subset_of_candidates_and_caps(
    evidence: list[DayEvidence], existing_year: int
) -> None:
    verdict = classify_month_drift(
        _MONTH,
        evidence,
        existing_year_overlay_days=existing_year,
        detected_at="2026-07-04T00:00:00+00:00",
    )
    flagged_days = {flag.day for flag in verdict.flagged}
    candidates = set(verdict.candidates)
    assert flagged_days <= candidates
    assert len(verdict.flagged) <= MAX_FLAGS_PER_MONTH
    by_day = {row.day: row for row in evidence}
    for day in flagged_days:
        row = by_day[day]
        assert row.failed_windows_touching == 0
        assert row.zero_bars_all_indices is True
        assert row.indices_with_month_data == 3
        assert row.clean_success_windows >= 1
    if verdict.over_month_cap or verdict.over_year_cap:
        assert verdict.flagged == ()
    if verdict.flagged:
        assert existing_year + len(verdict.flagged) <= MAX_OVERLAY_DAYS_PER_YEAR
    # Every candidate lands in exactly one outcome bucket (flagged/vetoed/
    # no-evidence) unless a cap refused the whole month.
    if not (verdict.over_month_cap or verdict.over_year_cap):
        buckets = flagged_days | set(verdict.vetoed_failed) | set(verdict.no_evidence)
        assert buckets == candidates


@given(
    d=st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 12, 31)),
    overlay_offsets=st.sets(st.integers(min_value=-10, max_value=10), max_size=8),
    include_d=st.booleans(),
)
def test_remove_only_invariant(d: date, overlay_offsets: set[int], include_d: bool) -> None:
    overlay = {d + timedelta(days=off) for off in overlay_offsets}
    if include_d:
        overlay.add(d)
    observed = frozenset(overlay)
    verdict = is_trading_day_observed(d, observed=observed)
    # REMOVE-ONLY: an observed verdict of True implies the derived calendar
    # already said True — no overlay can flip False -> True.
    if verdict:
        assert is_trading_day(d)
    # And subtraction is exact: overlaid -> always False.
    if d in observed:
        assert verdict is False

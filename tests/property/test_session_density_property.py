"""Property tests for `brutex.data.session_density` (DEC-SESSION-DENSITY-CENSUS-001).

Hypothesis invariants over the pure census classifier: total robustness (never
raises on timestamp multisets anchored ANYWHERE in the full representable
``datetime`` domain — review round-3 widened the previously-undisclosed
2019-2027 bound after the classifier was made total at the boundary), deletion
monotonicity (removing more interior bars never un-flags a flagged session),
the full-session never-flag law, and the abstain/judged partition of the
walked span.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from brutex.data.session_density import classify_session_density
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_FAR_FUTURE = date(2026, 12, 31)
_WED = date(2024, 1, 3)  # a regular interior Wednesday


def _minute_ts(d: date, minute_of_day: int) -> datetime:
    hh, mm = divmod(minute_of_day, 60)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC)


def _full_session(d: date) -> list[datetime]:
    return [_minute_ts(d, 9 * 60 + 15 + i) for i in range(375)]


@st.composite
def _anchored_multisets(draw: st.DrawFn) -> list[datetime]:
    """Timestamp multisets anchored anywhere in the FULL representable domain.

    The anchor ranges over ``[datetime.min, datetime.max]`` (UTC-aware) so the
    two round-3 boundary classes — the near-``datetime.max`` IST-conversion
    overflow and the ``date.max`` span-walk overflow — are genuinely explored;
    per-example jitter is span-BOUNDED (±200 days, boundary-clamped to the
    anchor) purely for walk-cost honesty: the classifier's documented
    O(days-in-span) walk makes a year-1→year-9999 multiset seconds-slow, and
    span LENGTH is not a totality class (the deterministic cross-boundary pins
    live in tests/unit/test_session_density.py TestDatetimeBoundaryTotality).
    """
    anchor = draw(st.datetimes(timezones=st.just(UTC)))
    deltas = draw(
        st.lists(
            st.timedeltas(min_value=timedelta(days=-200), max_value=timedelta(days=200)),
            max_size=60,
        )
    )
    out: list[datetime] = []
    for delta in deltas:
        try:
            out.append(anchor + delta)
        except OverflowError:
            out.append(anchor)  # boundary-clamped: keep the anchor itself
    return out


@given(stamps=_anchored_multisets())
@settings(deadline=None, max_examples=60)
def test_classifier_never_raises_on_arbitrary_multisets(stamps: list[datetime]) -> None:
    """Total over any timestamp multiset across the FULL representable domain
    — duplicates, out-of-coverage years, weekends, boundary-adjacent stamps,
    any order — the classifier returns a verdict, never raises (an
    IST-conversion overflow folds into the counted stamps_out_of_range skip)."""
    verdict = classify_session_density(stamps, today_ist=_FAR_FUTURE)
    assert verdict.sessions_checked >= 0
    assert verdict.stamps_out_of_range >= 0
    assert verdict.stamps_out_of_range <= len(stamps)


@given(
    keep=st.sets(st.integers(min_value=0, max_value=374), max_size=340),
)
@settings(deadline=None, max_examples=60)
def test_deleting_more_interior_bars_never_unflags(keep: set[int]) -> None:
    """Monotonicity: if the interior Wednesday flags with bar-set K, every
    subset of K (drop one more bar) still flags — a hole can only deepen."""
    open_minute = 9 * 60 + 15
    wed_bars = [_minute_ts(_WED, open_minute + i) for i in sorted(keep)]
    bracket = _full_session(date(2024, 1, 2)) + _full_session(date(2024, 1, 4))
    base = classify_session_density(bracket + wed_bars, today_ist=_FAR_FUTURE)
    if not base.flags:
        return
    smaller = wed_bars[:-1] if wed_bars else []
    deeper = classify_session_density(bracket + smaller, today_ist=_FAR_FUTURE)
    assert len(deeper.flags) == 1
    assert deeper.flags[0].observed <= base.flags[0].observed


@given(d=st.dates(min_value=date(2023, 1, 2), max_value=date(2025, 12, 30)))
@settings(deadline=None, max_examples=60)
def test_a_full_regular_session_never_flags(d: date) -> None:
    """Bracketing ANY interior regular trading day with full sessions on the
    two calendar neighbors: a dense day never flags at any position."""
    bars = (
        _full_session(d - timedelta(days=1))
        + _full_session(d)
        + _full_session(d + timedelta(days=1))
    )
    verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
    flagged_days = {f.session for f in verdict.flags} | {f.session for f in verdict.known_irregular}
    # 2023-2025 holds no extended session (465 min lives only on 2021-02-24)
    # and a Muhurat interior day abstains — a dense 375-bar day never flags.
    assert d not in flagged_days


@given(
    days=st.lists(
        st.dates(min_value=date(2024, 1, 1), max_value=date(2024, 3, 31)),
        min_size=1,
        max_size=8,
        unique=True,
    )
)
@settings(deadline=None, max_examples=60)
def test_abstains_partition_the_walked_span(days: list[date]) -> None:
    """judged + Σ(abstain counts) == the number of walked calendar dates."""
    bars = [_minute_ts(d, 12 * 60) for d in days]
    verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
    walked = (max(days) - min(days)).days + 1
    assert verdict.sessions_checked + sum(n for _, n in verdict.abstained) == walked

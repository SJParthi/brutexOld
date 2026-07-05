"""Daily-open substitution observe — detector/emitter/guard (DEC-DAILY-OPEN-SUBSTITUTION-FLAG-001).

The derived 1-day ``open`` silently substitutes the earliest PRESENT minute
when the 09:15 IST bar is absent — the implicit ``open.first()`` inside
:func:`brutex.data.tf_resampler.resample_1m_to_tf` (no coded branch). This
suite covers the OBSERVE-ONLY flag over that behavior:

* :func:`detect_daily_open_substitutions` — the pure scan (forming-session
  cutoff alignment with ``resample_1m_to_daily``, Muhurat abstention for BOTH
  the evening 2020-2024 shape and the afternoon 2025-10-21 shape — since
  DEC-INGEST-BOUNDARY-INTEGRITY-001 (#1294) the calendar-aware ingest filter
  KEEPS evening-Muhurat bars, so both shapes can genuinely reach the lake —
  order-independence, the exact ``!= 09:15`` predicate incl. a stray pre-09:15
  bar);
* :func:`emit_daily_open_substitutions` — the log taxonomy (per-date WARNs
  gated to dense, capped at :data:`MAX_OPEN_SUBSTITUTION_WARNS`; count-only
  aggregate for illiquid; total silence at zero);
* :func:`observe_daily_open_substitutions` — the ONE guarded production entry
  point (a poisoned frame logs ``daily_open.substitution_log_error`` and
  returns 0 — the observe-only diagnostic can NEVER break the prebuild;
  mirrors the ``emit_underlying_all_empty`` precedent).

Special-weekend sessions (e.g. the 2026-02-01 Budget Sunday) open at regular
09:15 hours and need no abstention — deliberately not special-cased.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest
from brutex.calendars.nse import MUHURAT_SESSIONS
from brutex.data.spot_daily_resample import (
    MAX_OPEN_SUBSTITUTION_WARNS,
    DailyOpenSubstitutionScan,
    detect_daily_open_substitutions,
    emit_daily_open_substitutions,
    observe_daily_open_substitutions,
)
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

#: 09:15 IST == 03:45 UTC — the regular session open.
_IST_OFFSET = timedelta(hours=5, minutes=30)

#: A far-future cutoff so every fixture session is COMPLETE unless a test
#: deliberately exercises the forming-session drop.
_FAR_BEFORE = date(2030, 1, 1)


def _session_1m(
    session_date: date,
    *,
    first_minute: time = time(9, 15),
    n: int = 5,
) -> pl.DataFrame:
    """*n* consecutive 1-minute bars starting at *first_minute* IST."""
    open_ist = datetime.combine(session_date, first_minute)
    base_utc = (open_ist - _IST_OFFSET).replace(tzinfo=UTC)
    ts = [base_utc + timedelta(minutes=i) for i in range(n)]
    return pl.DataFrame(
        {
            "ts_utc": ts,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [10 + i for i in range(n)],
        }
    )


def _concat(*frames: pl.DataFrame) -> pl.DataFrame:
    return pl.concat(list(frames)).sort("ts_utc")


# --------------------------------------------------------------------------- #
# detect_daily_open_substitutions — the pure scan
# --------------------------------------------------------------------------- #


class TestDetectDailyOpenSubstitutions:
    def test_full_session_not_flagged(self) -> None:
        scan = detect_daily_open_substitutions(
            _session_1m(date(2024, 1, 2)), before=_FAR_BEFORE
        )
        assert scan == DailyOpenSubstitutionScan(substituted=(), sessions=1, muhurat_abstained=0)
        assert scan.count == 0

    def test_missing_0915_flags_date_and_first_minute(self) -> None:
        """THE core pin: a 09:16-start session flags with its actual first minute."""
        scan = detect_daily_open_substitutions(
            _session_1m(date(2024, 1, 2), first_minute=time(9, 16)), before=_FAR_BEFORE
        )
        assert scan.substituted == ((date(2024, 1, 2), time(9, 16)),)
        assert scan.sessions == 1
        assert scan.count == 1

    def test_before_cutoff_excludes_forming_session(self) -> None:
        """A session on/after *before* is neither flagged nor counted — the SAME
        cutoff ``resample_1m_to_daily`` applies, so the scan flags only sessions
        actually written as final daily bars."""
        d = date(2024, 1, 2)
        scan = detect_daily_open_substitutions(
            _session_1m(d, first_minute=time(9, 16)), before=d
        )
        assert scan == DailyOpenSubstitutionScan(substituted=(), sessions=0, muhurat_abstained=0)

    def test_muhurat_evening_session_abstains(self) -> None:
        """2024-11-01 evening Muhurat (18:00 IST open) — abstained, counted.

        Since #1294's calendar-aware ingest filter KEEPS evening-Muhurat bars,
        this shape genuinely reaches the lake — the abstention is load-bearing.
        """
        d = date(2024, 11, 1)
        assert d in MUHURAT_SESSIONS  # calendar ground truth, not fabricated here
        scan = detect_daily_open_substitutions(
            _session_1m(d, first_minute=time(18, 0)), before=_FAR_BEFORE
        )
        assert scan == DailyOpenSubstitutionScan(substituted=(), sessions=1, muhurat_abstained=1)

    def test_muhurat_afternoon_session_abstains(self) -> None:
        """2025-10-21 AFTERNOON Muhurat (13:45 IST open) — the production-real
        false positive the abstention exists to suppress (an in-window 13:45
        open would otherwise read as a substituted daily open)."""
        d = date(2025, 10, 21)
        assert d in MUHURAT_SESSIONS
        scan = detect_daily_open_substitutions(
            _session_1m(d, first_minute=time(13, 45)), before=_FAR_BEFORE
        )
        assert scan == DailyOpenSubstitutionScan(substituted=(), sessions=1, muhurat_abstained=1)

    def test_mixed_sessions_exact_tuple(self) -> None:
        clean = _session_1m(date(2024, 1, 2))
        substituted = _session_1m(date(2024, 1, 3), first_minute=time(9, 17))
        muhurat = _session_1m(date(2024, 11, 1), first_minute=time(18, 0))
        scan = detect_daily_open_substitutions(
            _concat(clean, substituted, muhurat), before=_FAR_BEFORE
        )
        assert scan.substituted == ((date(2024, 1, 3), time(9, 17)),)
        assert scan.sessions == 3
        assert scan.muhurat_abstained == 1

    def test_empty_frame_zeroes(self) -> None:
        empty = _session_1m(date(2024, 1, 2)).clear()
        scan = detect_daily_open_substitutions(empty, before=_FAR_BEFORE)
        assert scan == DailyOpenSubstitutionScan(substituted=(), sessions=0, muhurat_abstained=0)

    def test_pre_0915_bar_flags_with_actual_first_minute(self) -> None:
        """The predicate is ``!= 09:15`` EXACTLY: a stray pre-09:15 bar (should
        not survive the ingest session filter) also flags, with the real first
        minute in the payload — never silently trusted."""
        scan = detect_daily_open_substitutions(
            _session_1m(date(2024, 1, 2), first_minute=time(9, 14)), before=_FAR_BEFORE
        )
        assert scan.substituted == ((date(2024, 1, 2), time(9, 14)),)

    def test_unsorted_input_order_independent(self) -> None:
        """The per-session first minute is ``min(ts)``, never a positional
        first row — a shuffled frame yields the identical scan."""
        frame = _concat(
            _session_1m(date(2024, 1, 2), first_minute=time(9, 16)),
            _session_1m(date(2024, 1, 3)),
        )
        shuffled = frame.sort("open", descending=True)
        assert detect_daily_open_substitutions(
            shuffled, before=_FAR_BEFORE
        ) == detect_daily_open_substitutions(frame, before=_FAR_BEFORE)

    def test_multiple_substituted_ascending_by_date(self) -> None:
        frame = _concat(
            _session_1m(date(2024, 1, 5), first_minute=time(9, 30)),
            _session_1m(date(2024, 1, 3), first_minute=time(9, 16)),
        )
        scan = detect_daily_open_substitutions(frame, before=_FAR_BEFORE)
        assert scan.substituted == (
            (date(2024, 1, 3), time(9, 16)),
            (date(2024, 1, 5), time(9, 30)),
        )

    @given(
        dates=st.lists(
            st.dates(min_value=date(2021, 1, 4), max_value=date(2021, 3, 31)),
            min_size=1,
            max_size=4,
            unique=True,
        ),
        extra_minutes=st.lists(
            st.integers(min_value=1, max_value=374), min_size=0, max_size=5, unique=True
        ),
        drop_index=st.integers(min_value=0, max_value=3),
    )
    def test_sessions_containing_0915_never_flag_and_dropping_it_flags(
        self, dates: list[date], extra_minutes: list[int], drop_index: int
    ) -> None:
        """Property: any non-Muhurat session CONTAINING its 09:15 bar never
        flags; removing the 09:15 bar from one session (leaving later minutes)
        flags exactly that date. Jan-Mar 2021 contains no Muhurat date
        (calendar ground truth — 2021's is 2021-11-04)."""

        def _frame(with_0915: set[date]) -> pl.DataFrame:
            frames = []
            for d in dates:
                open_utc = (datetime.combine(d, time(9, 15)) - _IST_OFFSET).replace(tzinfo=UTC)
                minutes = sorted({0, *extra_minutes} if d in with_0915 else {*extra_minutes, 1})
                frames.append(
                    pl.DataFrame(
                        {
                            "ts_utc": [open_utc + timedelta(minutes=m) for m in minutes],
                            "open": [1.0] * len(minutes),
                            "high": [1.0] * len(minutes),
                            "low": [1.0] * len(minutes),
                            "close": [1.0] * len(minutes),
                            "volume": [1] * len(minutes),
                        }
                    )
                )
            return _concat(*frames)

        assert all(d not in MUHURAT_SESSIONS for d in dates)
        all_clean = detect_daily_open_substitutions(_frame(set(dates)), before=_FAR_BEFORE)
        assert all_clean.count == 0
        assert all_clean.sessions == len(dates)

        victim = sorted(dates)[drop_index % len(dates)]
        one_dropped = detect_daily_open_substitutions(
            _frame(set(dates) - {victim}), before=_FAR_BEFORE
        )
        assert [d for d, _ in one_dropped.substituted] == [victim]


# --------------------------------------------------------------------------- #
# emit_daily_open_substitutions — the log taxonomy
# --------------------------------------------------------------------------- #


def _scan(n: int) -> DailyOpenSubstitutionScan:
    return DailyOpenSubstitutionScan(
        substituted=tuple((date(2024, 1, 1) + timedelta(days=i), time(9, 16)) for i in range(n)),
        sessions=n,
        muhurat_abstained=0,
    )


class TestEmitDailyOpenSubstitutions:
    def test_dense_warns_per_date_plus_aggregate(self) -> None:
        with capture_logs() as logs:
            emit_daily_open_substitutions(_scan(2), symbol="NSE-NIFTY", dense=True)
        per_date = [e for e in logs if e["event"] == "daily_open.substituted"]
        assert len(per_date) == 2
        assert per_date[0]["log_level"] == "warning"
        assert per_date[0]["session_date"] == "2024-01-01"
        assert per_date[0]["first_minute_ist"] == "09:16"
        agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"]
        assert len(agg) == 1
        assert agg[0]["log_level"] == "warning"
        assert agg[0]["count"] == 2
        assert agg[0]["warns_capped"] is False

    def test_dense_cap_at_20_with_true_count(self) -> None:
        with capture_logs() as logs:
            emit_daily_open_substitutions(_scan(25), symbol="NSE-NIFTY", dense=True)
        per_date = [e for e in logs if e["event"] == "daily_open.substituted"]
        assert len(per_date) == MAX_OPEN_SUBSTITUTION_WARNS == 20
        agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"]
        assert agg[0]["count"] == 25  # the TRUE count — the cap never lies
        assert agg[0]["warns_capped"] is True

    def test_illiquid_aggregate_only_info(self) -> None:
        """The operator-mandated count-only path for a non-dense symbol."""
        with capture_logs() as logs:
            emit_daily_open_substitutions(_scan(2), symbol="NSE-RELIANCE", dense=False)
        assert [e for e in logs if e["event"] == "daily_open.substituted"] == []
        agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"]
        assert len(agg) == 1
        assert agg[0]["log_level"] == "info"
        assert agg[0]["count"] == 2
        assert agg[0]["dense"] is False
        assert agg[0]["warns_capped"] is False

    def test_zero_count_emits_nothing(self) -> None:
        """A healthy lake's log surface is byte-identical to before the feature."""
        with capture_logs() as logs:
            emit_daily_open_substitutions(
                DailyOpenSubstitutionScan(substituted=(), sessions=5, muhurat_abstained=1),
                symbol="NSE-NIFTY",
                dense=True,
            )
        assert logs == []

    def test_aggregate_carries_sessions_and_muhurat(self) -> None:
        scan = DailyOpenSubstitutionScan(
            substituted=((date(2024, 1, 2), time(9, 16)),),
            sessions=7,
            muhurat_abstained=2,
        )
        with capture_logs() as logs:
            emit_daily_open_substitutions(scan, symbol="NSE-NIFTY", dense=False)
        agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"][0]
        assert (agg["sessions"], agg["muhurat_abstained"]) == (7, 2)


# --------------------------------------------------------------------------- #
# observe_daily_open_substitutions — the guarded production entry point
# --------------------------------------------------------------------------- #


class TestObserveDailyOpenSubstitutions:
    def test_success_emits_and_returns_count(self) -> None:
        frame = _session_1m(date(2024, 1, 2), first_minute=time(9, 16))
        with capture_logs() as logs:
            count = observe_daily_open_substitutions(
                frame, before=_FAR_BEFORE, symbol="NSE-NIFTY", dense=True
            )
        assert count == 1
        assert [e for e in logs if e["event"] == "daily_open.substituted"] != []

    def test_poisoned_frame_never_raises_returns_zero(self) -> None:
        """F-1 (mandatory fold): the WHOLE detect+emit pass is guarded — an
        unexpected error (here: a frame missing ``ts_utc``) logs the error
        event and returns 0, NEVER failing the prebuild that hosts it."""
        poisoned = pl.DataFrame({"open": [1.0]})
        with capture_logs() as logs:
            count = observe_daily_open_substitutions(
                poisoned, before=_FAR_BEFORE, symbol="NSE-NIFTY", dense=True
            )
        assert count == 0
        errors = [e for e in logs if e["event"] == "daily_open.substitution_log_error"]
        assert len(errors) == 1
        assert errors[0]["symbol"] == "NSE-NIFTY"

    def test_clean_scan_returns_zero_silently(self) -> None:
        with capture_logs() as logs:
            count = observe_daily_open_substitutions(
                _session_1m(date(2024, 1, 2)), before=_FAR_BEFORE, symbol="NSE-NIFTY", dense=True
            )
        assert count == 0
        assert logs == []


def test_scan_count_property() -> None:
    assert _scan(3).count == 3
    assert _scan(0).count == 0


def test_cap_constant_is_twenty() -> None:
    """The #919-precedent cap — a silent drift of the constant fails here."""
    assert MAX_OPEN_SUBSTITUTION_WARNS == 20


@pytest.mark.parametrize("n", [0, 1])
def test_emit_boundary_around_zero(n: int) -> None:
    with capture_logs() as logs:
        emit_daily_open_substitutions(_scan(n), symbol="S", dense=True)
    assert bool(logs) is (n > 0)

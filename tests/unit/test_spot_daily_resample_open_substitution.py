"""Daily-open substitution observe — detector/emitter/guard (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001).

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

DRIFT PIN (review fold): main's read-side /completeness counter
(DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001, MIGRATION.md #1267) counts the SAME
named concept with deliberately DIFFERENT rules (Muhurat counted there,
abstained here; strict ``>`` there, ``!=`` here) — the expected disagreement
is ENCODED by ``test_muhurat_divergence_pin_vs_honesty_counter_1267`` so a
future silent "reconciliation" of either side fails loudly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

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
        scan = detect_daily_open_substitutions(_session_1m(date(2024, 1, 2)), before=_FAR_BEFORE)
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
        scan = detect_daily_open_substitutions(_session_1m(d, first_minute=time(9, 16)), before=d)
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

    def test_illiquid_above_cap_warns_capped_false(self) -> None:
        """Mutant killer (review fold): ``warns_capped`` is the DENSE-channel
        truth — a non-dense symbol emits ZERO per-date WARNs, so even an
        above-cap count reads ``warns_capped=False`` (nothing existed to cap;
        claiming True would be exactly the misleading-log lie the field's
        "the cap never lies" contract forbids — Golden Rule #1). Kills the
        ``dense and`` → dropped mutant that every count<=20 fixture tolerated."""
        with capture_logs() as logs:
            emit_daily_open_substitutions(_scan(25), symbol="NSE-XYZ", dense=False)
        assert [e for e in logs if e["event"] == "daily_open.substituted"] == []
        agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"]
        assert len(agg) == 1
        assert agg[0]["count"] == 25  # the TRUE count still travels
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
        agg = next(e for e in logs if e["event"] == "daily_open.substituted_aggregate")
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


# --------------------------------------------------------------------------- #
# DRIFT PIN vs DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 (MIGRATION.md #1267)
# --------------------------------------------------------------------------- #


def test_muhurat_divergence_pin_vs_honesty_counter_1267(tmp_path: Path) -> None:
    """The DELIBERATE divergence between the two open-substitution surfaces.

    main ships a read-side daily-open substitution counter
    (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001, MIGRATION.md #1267 — the
    per-session CTE in ``db_view.summarize_spot_datasets``, surfaced as the
    /completeness "Open substituted N/M" column), and this PR ships the
    prebuild detector — SAME named concept, CONSCIOUSLY different rules (see
    the ``detect_daily_open_substitutions`` docstring): #1267 COUNTS a
    Muhurat session (strict ``>`` — any off-canonical open honestly means
    "the derived daily open is not the 09:15 price"), while this detector
    ABSTAINS on it (operator mandate — a Muhurat open is off-09:15 BY
    DESIGN, so a pipeline WARN would be a false positive).

    Over ONE lake holding a 09:16-start non-Muhurat session PLUS the
    calendar-verified 2025-10-21 afternoon Muhurat (13:45 IST), the two
    surfaces therefore DISAGREE: /completeness reads 2, the prebuild
    headline reads 1 (+1 ``muhurat_abstained``). This pin ENCODES that
    expected disagreement so a future silent "reconciliation" of either side
    fails loudly — actually aligning the predicates is a named follow-up
    decision, never a drive-by edit (CLAUDE.md Rule 11 COMMON honesty).
    """
    import duckdb
    from brutex.dashboard.db_view import summarize_spot_datasets
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    regular = date(2025, 10, 20)
    muhurat = date(2025, 10, 21)
    # Calendar ground truth, not fabricated here: the abstention keys ONLY on
    # Muhurat membership, so the fixture pins exactly that membership split.
    assert regular not in MUHURAT_SESSIONS
    assert muhurat in MUHURAT_SESSIONS

    def _bars(session_date: date, first_minute: time) -> list[CandleBar]:
        open_ist = datetime.combine(session_date, first_minute)
        base_utc = (open_ist - _IST_OFFSET).replace(tzinfo=UTC)
        return [
            CandleBar(
                timestamp=base_utc + timedelta(minutes=i),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=10,
                open_interest=None,
            )
            for i in range(3)
        ]

    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[*_bars(regular, time(9, 16)), *_bars(muhurat, time(13, 45))],
    )

    # Surface 1 — the #1267 read-side counter over the REAL lake: the Muhurat
    # session COUNTS (strict `>` 09:15), so both sessions are substituted.
    rows = summarize_spot_datasets(duckdb.connect(), tmp_path)
    row = next(r for r in rows if r["segment"] == "INDEX" and r["interval"] == "1minute")
    assert row["open_substituted_days"] == 2
    assert row["session_days"] == 2

    # Surface 2 — THIS PR's prebuild detector over the same sessions: the
    # Muhurat session ABSTAINS, so only the 09:16 non-Muhurat session flags.
    frame = _concat(
        _session_1m(regular, first_minute=time(9, 16), n=3),
        _session_1m(muhurat, first_minute=time(13, 45), n=3),
    )
    scan = detect_daily_open_substitutions(frame, before=_FAR_BEFORE)
    assert scan.substituted == ((regular, time(9, 16)),)
    assert scan.count == 1
    assert scan.muhurat_abstained == 1

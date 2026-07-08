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

ALIGNMENT PIN (DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the named follow-up the
former drift pin deferred to): the read-side /completeness counter
(DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001, MIGRATION.md #1267) and this
detector now share the ONE per-session predicate
:func:`brutex.data.spot_daily_resample.classify_daily_open` (Muhurat abstains
on BOTH; ``!=`` 09:15 on BOTH, pre-09:15 counts on BOTH). The agreement is
ENCODED by ``test_predicate_alignment_pin_vs_honesty_counter_1267`` +
``test_sql_cte_equivalent_to_classifier_over_one_lake`` so a future silent
predicate edit on EITHER side fails loudly; the ONE remaining deliberate
POPULATION difference (the detector's forming-session cutoff vs the census's
all-stored-sessions scope) is pinned by
``test_population_cutoff_divergence_pin``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.calendars.nse import MUHURAT_SESSIONS
from brutex.data.models import CandleBar
from brutex.data.spot_daily_resample import (
    MAX_OPEN_SUBSTITUTION_WARNS,
    DailyOpenSubstitutionScan,
    classify_daily_open,
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
# classify_daily_open — the ONE COMMON per-session predicate
# (DEC-DAILY-OPEN-PREDICATE-ALIGN-001)
# --------------------------------------------------------------------------- #


class TestClassifyDailyOpen:
    def test_exact_0915_is_ok(self) -> None:
        assert classify_daily_open(date(2024, 1, 2), time(9, 15)) == "ok"

    def test_late_first_minute_is_substituted(self) -> None:
        assert classify_daily_open(date(2024, 1, 2), time(9, 16)) == "substituted"

    def test_pre_0915_first_minute_is_substituted(self) -> None:
        # The `!=` rule: a stray pre-09:15 first bar COUNTS (never silently
        # trusted) — the aligned rule both surfaces now share.
        assert classify_daily_open(date(2024, 1, 2), time(9, 14)) == "substituted"

    def test_muhurat_abstains_evening_and_afternoon(self) -> None:
        assert date(2024, 11, 1) in MUHURAT_SESSIONS
        assert date(2025, 10, 21) in MUHURAT_SESSIONS
        assert classify_daily_open(date(2024, 11, 1), time(18, 0)) == "muhurat_abstained"
        assert classify_daily_open(date(2025, 10, 21), time(13, 45)) == "muhurat_abstained"

    def test_muhurat_precedes_the_0915_check(self) -> None:
        # Predicate ORDER pin: a Muhurat session abstains even at an exact
        # 09:15 first minute — Muhurat membership is judged first.
        assert classify_daily_open(date(2025, 10, 21), time(9, 15)) == "muhurat_abstained"

    def test_seconds_precision_is_substituted(self) -> None:
        # 09:15:30 is NOT the canonical 09:15:00 — the exact-instant rule,
        # mirroring the SQL `first_ist <> ist_date + 555 minutes` comparison.
        assert classify_daily_open(date(2024, 1, 2), time(9, 15, 30)) == "substituted"


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
# ALIGNMENT PIN vs DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 (MIGRATION.md #1267)
# — DEC-DAILY-OPEN-PREDICATE-ALIGN-001
# --------------------------------------------------------------------------- #


def _lake_bars(session_date: date, first_minute: time, n: int = 3) -> list[CandleBar]:
    """*n* real :class:`CandleBar`\\ s starting at *first_minute* IST — lake fixtures."""
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
        for i in range(n)
    ]


def _write_nifty_minutes(lake_root: Path, bars: list[CandleBar]) -> str:
    """Write one NSE/INDEX/NIFTY ``1minute`` lake; return its parquet glob.

    The writer requires single-``(year, month)`` batches (its partition
    contract), so the bars are grouped per UTC month before writing.
    """
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=lake_root)
    by_month: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        by_month.setdefault((bar.timestamp.year, bar.timestamp.month), []).append(bar)
    for batch in by_month.values():
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=batch
        )
    base = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    return base.as_posix() + "/**/*.parquet"


def test_predicate_alignment_pin_vs_honesty_counter_1267(tmp_path: Path) -> None:
    """The two open-substitution surfaces now AGREE on the same lake.

    DEC-DAILY-OPEN-PREDICATE-ALIGN-001 — the named follow-up decision the
    former ``test_muhurat_divergence_pin_vs_honesty_counter_1267`` drift pin
    deferred to (this test CONSCIOUSLY rewrites that pin): the read-side
    /completeness counter (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001, #1267 —
    the per-session CTE in ``db_view.summarize_spot_datasets``) and the
    prebuild detector now share the ONE per-session predicate
    ``classify_daily_open`` — Muhurat abstains on BOTH surfaces (the operator
    mandate: a Muhurat open is off-09:15 BY DESIGN, so counting it flagged a
    healthy lake every year).

    Over ONE lake holding a 09:16-start non-Muhurat session PLUS the
    calendar-verified 2025-10-21 afternoon Muhurat (13:45 IST) — the exact
    fixture the old drift pin encoded a DISAGREEMENT (2 vs 1) over — both
    surfaces now read 1. A future silent predicate edit on EITHER side fails
    this pin loudly (CLAUDE.md Rule 11 COMMON honesty).
    """
    import duckdb
    from brutex.dashboard.db_view import summarize_spot_datasets

    regular = date(2025, 10, 20)
    muhurat = date(2025, 10, 21)
    # Calendar ground truth, not fabricated here: the abstention keys ONLY on
    # Muhurat membership, so the fixture pins exactly that membership split.
    assert regular not in MUHURAT_SESSIONS
    assert muhurat in MUHURAT_SESSIONS

    _write_nifty_minutes(
        tmp_path,
        [*_lake_bars(regular, time(9, 16)), *_lake_bars(muhurat, time(13, 45))],
    )

    # Surface 1 — the #1267 read-side counter over the REAL lake: the Muhurat
    # session now ABSTAINS in SQL too, so ONLY the 09:16 session is counted.
    rows = summarize_spot_datasets(duckdb.connect(), tmp_path)
    row = next(r for r in rows if r["segment"] == "INDEX" and r["interval"] == "1minute")
    assert row["open_substituted_days"] == 1
    assert row["session_days"] == 2

    # Surface 2 — the prebuild detector over the same sessions: identical
    # verdicts (the Muhurat session abstains, counted separately).
    frame = _concat(
        _session_1m(regular, first_minute=time(9, 16), n=3),
        _session_1m(muhurat, first_minute=time(13, 45), n=3),
    )
    scan = detect_daily_open_substitutions(frame, before=_FAR_BEFORE)
    assert scan.substituted == ((regular, time(9, 16)),)
    assert scan.count == 1 == row["open_substituted_days"]
    assert scan.sessions == 2 == row["session_days"]
    assert scan.muhurat_abstained == 1


def test_sql_cte_equivalent_to_classifier_over_one_lake(tmp_path: Path) -> None:
    """EQUIVALENCE PROOF: same lake ⇒ same per-session verdicts, both engines.

    DEC-DAILY-OPEN-PREDICATE-ALIGN-001: the census's SQL predicate
    (``first_ist <> ist_date + 555 minutes`` + the ``_MUHURAT_EXCLUSION_SQL``
    date exclusion) must be provably equivalent to the ONE COMMON
    ``classify_daily_open`` policy. Over one REAL lake exercising EVERY case
    class — exact-09:15 (ok) · normal-late 09:16 (substituted) · stray
    pre-09:15 09:14 (substituted) · afternoon Muhurat 13:45 (abstained) ·
    evening Muhurat 18:00 (abstained) — this test (a) recomputes each
    session's SQL verdict with the PRODUCTION fragments (`_IST_DATE_EXPR` /
    `_IST_TS_EXPR` / `_SESSION_OPEN_MINUTES` / `_MUHURAT_EXCLUSION_SQL` —
    imported, never re-derived) and asserts it equals the classifier's
    verdict per session, and (b) asserts the production
    ``summarize_spot_datasets`` total equals the detector's count.
    """
    import duckdb
    from brutex.dashboard.db_view import (
        _IST_DATE_EXPR,
        _IST_TS_EXPR,
        _MUHURAT_EXCLUSION_SQL,
        _SESSION_OPEN_MINUTES,
        summarize_spot_datasets,
    )

    evening_muhurat = date(2024, 11, 1)
    afternoon_muhurat = date(2025, 10, 21)
    assert evening_muhurat in MUHURAT_SESSIONS
    assert afternoon_muhurat in MUHURAT_SESSIONS
    sessions: dict[date, time] = {
        date(2025, 10, 14): time(9, 14),  # stray pre-09:15 first bar
        date(2025, 10, 15): time(9, 15),  # exact canonical open
        date(2025, 10, 20): time(9, 16),  # normal late open
        afternoon_muhurat: time(13, 45),  # afternoon Muhurat
        evening_muhurat: time(18, 0),  # evening Muhurat
    }
    glob = _write_nifty_minutes(
        tmp_path,
        [bar for d, first in sessions.items() for bar in _lake_bars(d, first)],
    )

    # (a) per-session verdict parity — the EXACT production SQL fragments.
    conn = duckdb.connect()
    sql = (
        "WITH per_session AS ("  # noqa: S608  # nosec B608 - static fragments from hardcoded production constants; path bound
        f"SELECT {_IST_DATE_EXPR} AS ist_date, "
        f"MIN({_IST_TS_EXPR}) AS first_ist "
        "FROM read_parquet(?, union_by_name=true) GROUP BY ist_date) "
        "SELECT ist_date, first_ist, (first_ist <> ist_date "
        f"+ INTERVAL '{_SESSION_OPEN_MINUTES} minutes'{_MUHURAT_EXCLUSION_SQL}) "
        "FROM per_session ORDER BY ist_date"
    )
    sql_rows = conn.execute(sql, [glob]).fetchall()
    assert len(sql_rows) == len(sessions)
    for ist_date, first_ist, sql_counted in sql_rows:
        verdict = classify_daily_open(ist_date, first_ist.time())
        assert first_ist.time() == sessions[ist_date]  # fixture integrity
        assert bool(sql_counted) == (verdict == "substituted"), (
            f"SQL/classifier drift on {ist_date}: sql={sql_counted} verdict={verdict}"
        )

    # (b) production totals parity — census vs detector over the same lake.
    rows = summarize_spot_datasets(duckdb.connect(), tmp_path)
    row = next(r for r in rows if r["segment"] == "INDEX" and r["interval"] == "1minute")
    frame = _concat(*(_session_1m(d, first_minute=first, n=3) for d, first in sessions.items()))
    scan = detect_daily_open_substitutions(frame, before=_FAR_BEFORE)
    assert row["open_substituted_days"] == scan.count == 2
    assert row["session_days"] == scan.sessions == 5
    assert scan.muhurat_abstained == 2
    assert scan.substituted == (
        (date(2025, 10, 14), time(9, 14)),
        (date(2025, 10, 20), time(9, 16)),
    )


def test_population_cutoff_divergence_pin(tmp_path: Path) -> None:
    """The ONE remaining deliberate divergence is POPULATION, not predicate.

    DEC-DAILY-OPEN-PREDICATE-ALIGN-001 honesty pin: the census counts ALL
    stored sessions (no forming-session cutoff), while the detector applies
    the ``ist_date < before`` cutoff (a still-forming session was never
    written as a final daily bar, so the prebuild headline must not flag
    it). Over one lake holding a COMPLETE late-open session plus a FORMING
    late-open session (on the ``before`` date), the census reads 2 and the
    detector reads 1 — they differ ONLY on the forming session, never on a
    session both examined.
    """
    import duckdb
    from brutex.dashboard.db_view import summarize_spot_datasets

    complete = date(2025, 10, 20)
    forming = date(2025, 10, 22)
    assert complete not in MUHURAT_SESSIONS
    assert forming not in MUHURAT_SESSIONS

    _write_nifty_minutes(
        tmp_path,
        [*_lake_bars(complete, time(9, 16)), *_lake_bars(forming, time(9, 16))],
    )
    rows = summarize_spot_datasets(duckdb.connect(), tmp_path)
    row = next(r for r in rows if r["segment"] == "INDEX" and r["interval"] == "1minute")
    assert row["open_substituted_days"] == 2  # census: ALL stored sessions

    frame = _concat(
        _session_1m(complete, first_minute=time(9, 16), n=3),
        _session_1m(forming, first_minute=time(9, 16), n=3),
    )
    scan = detect_daily_open_substitutions(frame, before=forming)
    assert scan.substituted == ((complete, time(9, 16)),)  # the forming session is cut off
    assert scan.count == 1
    assert scan.sessions == 1

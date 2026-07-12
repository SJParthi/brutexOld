"""Tests for `brutex.data.session_density` — the WARN-only bar-density census.

DEC-SESSION-DENSITY-CENSUS-001. 100% line+branch STANDALONE (the ci.yml
per-module gate): the deficiency law (abs-floor AND fraction, both boundary
sides), every abstain reason (out_of_coverage · non_trading · observed-overlay
· muhurat · forming_session · boundary), the interior zero-bar
``missing_session`` arm, the shape taxonomy + gap bounds, the ``tf_minutes>1``
scaling arm (review round-2 mutant killers — bucket division + both duration
clamps), the known-irregular INFO arm (2021-02-24's 375/465 + the 2020 halt
days), the fail-soft env knobs + the injected-override seams + the default-ON
kill switch, the cap-20 + suppressed-summary emit discipline, and the
error-guard twin (an emitter crash never propagates).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.calendars.nse import is_trading_day
from brutex.data import session_density as sd
from brutex.data.session_density import (
    SESSION_DENSITY_ABS_FLOOR,
    SESSION_DENSITY_ERROR_EVENT,
    SESSION_DENSITY_KILL_ENV,
    SESSION_DENSITY_KNOWN_EVENT,
    SESSION_DENSITY_LOW_EVENT,
    SESSION_DENSITY_MIN_FRACTION,
    SESSION_DENSITY_SUPPRESSED_EVENT,
    SESSION_DENSITY_WARN_CAP,
    SHAPE_INTRADAY_HOLE,
    SHAPE_MISSING_SESSION,
    SHAPE_SPARSE,
    SessionDensityFlag,
    classify_session_density,
    observe_session_density,
    session_density_census_enabled,
)
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")

#: A far-future forming-session cutoff so historical fixtures always judge.
_FAR_FUTURE = date(2026, 12, 31)


def _minute_ts(d: date, hh: int, mm: int) -> datetime:
    """A UTC-aware timestamp for the IST wall clock ``hh:mm`` on ``d``."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC)


def _session_minutes(
    d: date, *, skip: set[int] | None = None, open_minute: int = 9 * 60 + 15, count: int = 375
) -> list[datetime]:
    """One bar per session minute on ``d``, minus the ``skip`` minute-of-day set."""
    skipped = skip or set()
    out: list[datetime] = []
    for i in range(count):
        minute_of_day = open_minute + i
        if minute_of_day in skipped:
            continue
        out.append(_minute_ts(d, *divmod(minute_of_day, 60)))
    return out


# Regular NSE trading days used as fixtures (Tue/Wed/Thu, verified calendar).
_TUE = date(2024, 1, 2)
_WED = date(2024, 1, 3)
_THU = date(2024, 1, 4)


def _bracketed(middle_bars: list[datetime]) -> list[datetime]:
    """Full boundary sessions on Tue + Thu around *middle_bars* on Wednesday."""
    return _session_minutes(_TUE) + middle_bars + _session_minutes(_THU)


class TestKillSwitch:
    def test_default_on(self) -> None:
        assert session_density_census_enabled(None) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", " OFF ", "False"])
    def test_falsy_literals_disable(self, raw: str) -> None:
        assert session_density_census_enabled(raw) is False

    @pytest.mark.parametrize("raw", ["", "1", "on", "junk"])
    def test_everything_else_stays_on(self, raw: str) -> None:
        assert session_density_census_enabled(raw) is True

    def test_env_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSION_DENSITY_KILL_ENV, "off")
        assert session_density_census_enabled() is False
        monkeypatch.delenv(SESSION_DENSITY_KILL_ENV)
        assert session_density_census_enabled() is True


class TestTunableKnobs:
    """Fail-soft env parsing — malformed env NEVER crashes a pull."""

    def test_fraction_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", raising=False)
        assert (
            sd._tunable_fraction("BRUTEX_SESSION_DENSITY_MIN_FRACTION", 0.9)  # pyright: ignore[reportPrivateUsage]
            == 0.9
        )

    @pytest.mark.parametrize("raw", ["junk", "", "nan", "inf", "-0.5", "0", "1.5"])
    def test_fraction_junk_falls_back(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", raw)
        assert (
            sd._tunable_fraction("BRUTEX_SESSION_DENSITY_MIN_FRACTION", 0.9)  # pyright: ignore[reportPrivateUsage]
            == 0.9
        )

    def test_fraction_valid_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", "0.5")
        assert (
            sd._tunable_fraction("BRUTEX_SESSION_DENSITY_MIN_FRACTION", 0.9)  # pyright: ignore[reportPrivateUsage]
            == 0.5
        )

    def test_fraction_one_is_valid_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", "1.0")
        assert (
            sd._tunable_fraction("BRUTEX_SESSION_DENSITY_MIN_FRACTION", 0.9)  # pyright: ignore[reportPrivateUsage]
            == 1.0
        )

    def test_floor_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", raising=False)
        assert sd._tunable_floor("BRUTEX_SESSION_DENSITY_ABS_FLOOR", 30) == 30  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize("raw", ["junk", "", "0", "-5", "1.5"])
    def test_floor_junk_falls_back(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", raw)
        assert sd._tunable_floor("BRUTEX_SESSION_DENSITY_ABS_FLOOR", 30) == 30  # pyright: ignore[reportPrivateUsage]

    def test_floor_valid_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", "5")
        assert sd._tunable_floor("BRUTEX_SESSION_DENSITY_ABS_FLOOR", 30) == 5  # pyright: ignore[reportPrivateUsage]


class TestLargestMissingRun:
    def test_interior_run(self) -> None:
        present = set(range(10)) - {3, 4, 5}
        assert sd._largest_missing_run(present, 10) == (3, 5)  # pyright: ignore[reportPrivateUsage]

    def test_tail_run_wins_when_longer(self) -> None:
        present = set(range(10)) - {2, 6, 7, 8, 9}
        assert sd._largest_missing_run(present, 10) == (6, 9)  # pyright: ignore[reportPrivateUsage]

    def test_earliest_run_wins_ties(self) -> None:
        present = set(range(10)) - {1, 2, 7, 8}
        assert sd._largest_missing_run(present, 10) == (1, 2)  # pyright: ignore[reportPrivateUsage]

    def test_earliest_run_wins_trailing_tie(self) -> None:
        # Review round-3 mutant killer: an interior run TIED by a TRAILING
        # run (slots 1-2 vs 8-9) must report the EARLIEST run's bounds — the
        # trailing-arm `>` -> `>=` mutant flips this to (8, 9), contradicting
        # the docstring's "ties: the earliest run wins" law (the interior tie
        # arm above cannot see it: its tie never reaches the trailing arm).
        present = set(range(10)) - {1, 2, 8, 9}
        assert sd._largest_missing_run(present, 10) == (1, 2)  # pyright: ignore[reportPrivateUsage]

    def test_all_missing(self) -> None:
        assert sd._largest_missing_run(frozenset(), 5) == (0, 4)  # pyright: ignore[reportPrivateUsage]

    def test_gap_free_input_raises_loudly(self) -> None:
        with pytest.raises(ValueError, match="no missing bucket slot"):
            sd._largest_missing_run(set(range(5)), 5)  # pyright: ignore[reportPrivateUsage]


class TestDeficiencyLaw:
    """The two-condition AND + the 30-min shape cut — every boundary pinned on
    BOTH sides: abs floor at deficit 30-flags/29-passes; the fraction cutoff at
    the real ``ceil(375*0.9)=338`` boundary — observed 338 passes / 337 flags
    (337 is also the ceil-vs-floor discriminator: ``floor(337.5)=337`` would
    wrongly pass it); the intraday-hole shape cut at largest gap
    30 → ``intraday_hole`` / 29 → ``sparse``. (Review round-1: the pre-fix
    345/330 pair straddled the boundary too loosely — three mutants survived.)
    """

    def test_45_minute_hole_flags(self) -> None:
        # 330 observed: deficit 45 >= 30 AND 330 < ceil(375*0.9)=338 → FLAG.
        hole = set(range(600, 645))  # 10:00-10:44 IST
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert flag.session == _WED
        assert (flag.expected, flag.observed) == (375, 330)
        assert flag.shape == SHAPE_INTRADAY_HOLE
        assert flag.largest_gap_minutes == 45
        assert flag.gap_start_ist == time(10, 0)
        assert flag.gap_end_ist == time(10, 45)
        assert flag.known_irregular is False

    def test_30_minute_hole_passes_fraction_arm(self) -> None:
        # 345 observed: deficit 30 >= 30 BUT 345 >= 338 → the fraction arm
        # saves it (deliberately below the 45-min..4-hr target class).
        hole = set(range(600, 630))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()
        assert verdict.sessions_checked == 1

    def test_observed_exactly_at_ceil_boundary_passes(self) -> None:
        # The fraction cutoff's EXACT boundary: 37 missing → observed 338 ==
        # ceil(375*0.9)=338, deficit 37 >= 30 — `observed >= ceil(...)` saves
        # it. Kills the `>=`→`>` boundary mutant on the fraction compare.
        hole = set(range(600, 637))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()
        assert verdict.sessions_checked == 1

    def test_observed_one_below_ceil_boundary_flags(self) -> None:
        # 38 missing → observed 337 < ceil(375*0.9)=338 → FLAG. The ceil-vs-
        # floor discriminator: floor(375*0.9)=floor(337.5)=337 would read
        # `337 >= 337` and wrongly pass — kills the ceil→floor mutant.
        hole = set(range(600, 638))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        assert (verdict.flags[0].expected, verdict.flags[0].observed) == (375, 337)

    def test_29_missing_bars_passes_abs_floor_arm(self) -> None:
        # Force a permissive fraction so ONLY the abs floor gates: 29 < 30.
        hole = set(range(600, 629))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)),
            min_fraction=1.0,
            today_ist=_FAR_FUTURE,
        )
        assert verdict.flags == ()

    def test_exactly_30_missing_bars_flags_under_permissive_fraction(self) -> None:
        # The abs-floor boundary: deficit == 30 flags (>=, not >) — AND the
        # 30-minute contiguous gap classifies INTRADAY_HOLE at the shape cut's
        # exact `>=` boundary (kills the `>=`→`>` shape-cut mutant, which
        # would misread this hole as `sparse`).
        hole = set(range(600, 630))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)),
            min_fraction=1.0,
            today_ist=_FAR_FUTURE,
        )
        assert len(verdict.flags) == 1
        assert verdict.flags[0].largest_gap_minutes == 30
        assert verdict.flags[0].shape == SHAPE_INTRADAY_HOLE

    def test_29_minute_largest_gap_reads_sparse(self) -> None:
        # The shape cut's other side: a FLAGGED session (deficit 38 >= 30,
        # observed 337 < 338) whose largest contiguous gap is 29 minutes reads
        # SPARSE, never intraday_hole — 9 extra isolated single-minute gaps
        # (every other minute over 700..717) push the deficit past both arms
        # without growing the largest run.
        hole = set(range(600, 629)) | set(range(700, 718, 2))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert flag.observed == 337
        assert flag.largest_gap_minutes == 29
        assert flag.shape == SHAPE_SPARSE

    def test_missing_interior_session_flags_at_zero(self) -> None:
        # The 1-session hole invisible to interior-gap's tolerance-5.
        verdict = classify_session_density(_bracketed([]), today_ist=_FAR_FUTURE)
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert flag.session == _WED
        assert flag.shape == SHAPE_MISSING_SESSION
        assert flag.fraction == 0.0
        assert flag.observed == 0
        assert flag.largest_gap_minutes == 375
        assert (flag.gap_start_ist, flag.gap_end_ist) == (time(9, 15), time(15, 30))

    def test_scattered_deficit_reads_sparse(self) -> None:
        # 40 isolated missing minutes (every other minute over 10:00-11:19):
        # deficit 40 >= 30, 335 < 338, largest gap 1 min < 30 → sparse.
        scattered = set(range(600, 680, 2))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=scattered)), today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert flag.shape == SHAPE_SPARSE
        assert flag.largest_gap_minutes == 1

    def test_full_session_never_flags(self) -> None:
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED)), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()
        assert verdict.known_irregular == ()
        assert verdict.sessions_checked == 1

    def test_off_session_strays_and_duplicates_never_inflate_density(self) -> None:
        # A hollow Wednesday (one real bar) + pre-open/post-close strays +
        # duplicate timestamps still reads observed == 1. The 15:30:00 stray is
        # the RIGHT-edge discriminator (review round-4 mutant kill): under the
        # half-open [open, close) session it is post-close, but an `<` -> `<=`
        # mutant of the in-session predicate lands it in the out-of-grid bucket
        # slot == expected and inflates observed to 2.
        one_bar = [_minute_ts(_WED, 9, 15)]
        strays = [
            _minute_ts(_WED, 9, 0),  # pre-open
            _minute_ts(_WED, 15, 30),  # exactly AT close — post-close (half-open)
            _minute_ts(_WED, 15, 45),  # post-close
            _minute_ts(_WED, 9, 15),  # duplicate of the real bar
        ]
        verdict = classify_session_density(_bracketed(one_bar + strays), today_ist=_FAR_FUTURE)
        assert len(verdict.flags) == 1
        assert verdict.flags[0].observed == 1

    def test_at_close_stray_on_extended_day_never_inflates_density(self) -> None:
        # The extended-day twin of the right-edge kill (review round-4): on the
        # 2021-02-24 extended session (close 17:00) a 17:00:00 stamp is
        # post-close — a hollow known-irregular day with one 09:15 bar plus a
        # 17:00:00 stray still reads observed == 1 (never 2).
        ext = date(2021, 2, 24)
        bars = [
            *_session_minutes(date(2021, 2, 23)),
            _minute_ts(ext, 9, 15),
            _minute_ts(ext, 17, 0),
            *_session_minutes(date(2021, 2, 25)),
        ]
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert len(verdict.known_irregular) == 1
        assert verdict.known_irregular[0].observed == 1

    def test_env_knobs_reach_the_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A 45-min hole passes under an env-loosened fraction (0.5).
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", "0.5")
        hole = set(range(600, 645))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()

    def test_default_constants_are_the_documented_values(self) -> None:
        assert SESSION_DENSITY_MIN_FRACTION == 0.90
        assert SESSION_DENSITY_ABS_FLOOR == 30
        assert SESSION_DENSITY_WARN_CAP == 20


class TestTfScaling:
    """The ``tf_minutes > 1`` arm of ``_judge_session`` (review round-2).

    Both production wirings hard-gate to ``1minute``, but ``tf_minutes`` is an
    advertised public parameter and ``expected_bars_for_session`` is
    TF-generic (derived-TF use is plausible) — so the tf-scaling arithmetic
    stays and gets teeth: the bucket division ``(m - open_min) // tf_minutes``
    and the gap-END clamp ``min((end_slot + 1) * tf, duration)`` at the
    truncated final bucket, from which the gap LENGTH now derives
    (``gap_end_minutes - start_slot * tf`` — review round-3, so length and
    bounds can never disagree). A regular session at tf=30 expects only ``ceil(375/30) = 13``
    buckets — below the default 30-bar abs floor — so these cases inject
    ``abs_floor`` (the round-2 injection seam that also makes the census
    golden hermetic).
    """

    def test_tf30_missing_session_engages_both_duration_clamps(self) -> None:
        # A fully-missing interior Wednesday at tf=30: the largest missing run
        # is slots 0..12, whose UNclamped scaled end is 13*30 = 390 > the
        # 375-minute session duration:
        #   * gap_end == 15:30 kills the gap-END clamp mutant (dropping
        #     min(..., duration) would read open + 390min = 15:45);
        #   * largest_gap_minutes == 375 rides the SAME clamp (the length
        #     derives from gap_end_minutes — review round-3), so an unclamped
        #     end would ALSO read a fabricated 390-minute gap.
        verdict = classify_session_density(
            _bracketed([]), tf_minutes=30, abs_floor=1, today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert flag.session == _WED
        assert (flag.expected, flag.observed) == (13, 0)
        assert flag.shape == SHAPE_MISSING_SESSION
        assert flag.fraction == 0.0
        assert flag.largest_gap_minutes == 375
        assert (flag.gap_start_ist, flag.gap_end_ist) == (time(9, 15), time(15, 30))

    def test_tf30_partial_tail_hole_clamps_gap_end_at_session_close(self) -> None:
        # Buckets 0..9 present (one bar at each 30-min bucket open), tail
        # buckets 10..12 missing: the run ENDS at the TRUNCATED final bucket
        # (slot 12 covers only 15:15..15:29), so the gap-END clamp engages
        # ((12+1)*30 = 390 > 375 → 15:30) and the gap LENGTH is the REAL
        # wall-clock span of those bounds — 2 full buckets + the 15-min tail
        # = 75, NOT the bucket-granular 90 (review round-3: the two fields on
        # one frozen flag can never disagree; unclamped gap_end would read
        # 15:45).
        present = [_minute_ts(_WED, *divmod(9 * 60 + 15 + slot * 30, 60)) for slot in range(10)]
        verdict = classify_session_density(
            _bracketed(present), tf_minutes=30, abs_floor=1, today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert (flag.expected, flag.observed) == (13, 10)
        assert flag.shape == SHAPE_INTRADAY_HOLE
        assert flag.largest_gap_minutes == 75  # 2 full + the 15-min tail — the real span
        assert flag.gap_start_ist == time(14, 15)
        assert flag.gap_end_ist == time(15, 30)  # NOT 15:45 — clamped at close
        # The internal-consistency law itself: length == gap_end − gap_start.
        assert flag.largest_gap_minutes == (15 * 60 + 30) - (14 * 60 + 15)

    def test_tf30_two_bars_in_one_bucket_count_one_observed(self) -> None:
        # The bucket-division mutant killer: 09:15 + 09:16 land in bucket 0
        # (ONE observed bucket, never two raw minute offsets) beside a lone
        # 12:15 bar in bucket 6 — dropping ``// tf_minutes`` would read
        # observed == 3 (raw offsets {0, 1, 180}) and a different largest run.
        bars = [
            _minute_ts(_WED, 9, 15),
            _minute_ts(_WED, 9, 16),  # same 30-min bucket as 09:15
            _minute_ts(_WED, 12, 15),  # bucket 6
        ]
        verdict = classify_session_density(
            _bracketed(bars), tf_minutes=30, abs_floor=1, today_ist=_FAR_FUTURE
        )
        assert len(verdict.flags) == 1
        flag = verdict.flags[0]
        assert (flag.expected, flag.observed) == (13, 2)
        # Largest missing run = buckets 7..12 (six buckets beat 1..5's five);
        # the run includes the TRUNCATED final bucket, so the length is the
        # real 12:45→15:30 span = 165 minutes (review round-3 consistency —
        # 5 full buckets + the 15-min tail, never the bucket-granular 180).
        assert flag.largest_gap_minutes == 165
        assert flag.gap_start_ist == time(12, 45)
        assert flag.gap_end_ist == time(15, 30)

    def test_injected_abs_floor_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The injection seam wins over the env knob: a permissive env floor of
        # 1 would flag the 45-min hole; the injected 100 saves it.
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", "1")
        hole = set(range(600, 645))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)),
            abs_floor=100,
            today_ist=_FAR_FUTURE,
        )
        assert verdict.flags == ()
        assert verdict.sessions_checked == 1

    def test_env_abs_floor_reaches_the_law_when_not_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The None fallback still reads the env knob (fail-soft law kept):
        # env floor 46 saves the 45-missing-bar hole that flags by default.
        monkeypatch.setenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", "46")
        hole = set(range(600, 645))
        verdict = classify_session_density(
            _bracketed(_session_minutes(_WED, skip=hole)), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()


class TestAbstains:
    def test_empty_input_is_the_zero_verdict(self) -> None:
        verdict = classify_session_density([], today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert verdict.known_irregular == ()
        assert verdict.sessions_checked == 0
        assert verdict.abstained == ()

    def test_boundary_dates_abstain(self) -> None:
        # Hollow FIRST and LAST covered dates (one bar each) never flag — a
        # mid-day-clamped / chunk-tiled window edge is not a hole.
        bars = [_minute_ts(_TUE, 12, 0), *_session_minutes(_WED), _minute_ts(_THU, 9, 15)]
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert dict(verdict.abstained)["boundary"] == 2
        assert verdict.sessions_checked == 1

    def test_single_covered_date_is_boundary_only(self) -> None:
        verdict = classify_session_density([_minute_ts(_TUE, 12, 0)], today_ist=_FAR_FUTURE)
        assert verdict.sessions_checked == 0
        assert verdict.abstained == (("boundary", 1),)

    def test_non_trading_interior_days_skip(self) -> None:
        # Fri 2024-01-05 .. Mon 2024-01-08: the weekend days are non_trading.
        bars = _session_minutes(date(2024, 1, 5)) + _session_minutes(date(2024, 1, 8))
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert dict(verdict.abstained)["non_trading"] == 2  # Sat + Sun
        assert verdict.sessions_checked == 0  # both covered dates are boundary

    def test_observed_overlay_day_reads_non_trading(self) -> None:
        # The remove-only overlay: an overlaid interior trading day is NOT a
        # session, so it can never false-flag as missing.
        verdict = classify_session_density(
            _bracketed([]), observed=frozenset({_WED}), today_ist=_FAR_FUTURE
        )
        assert verdict.flags == ()
        assert dict(verdict.abstained)["non_trading"] == 1

    def test_muhurat_interior_day_abstains(self) -> None:
        # 2024-11-01 is the Diwali Muhurat (18:00-19:00) — bracket it with the
        # sessions around it (Thu 2024-10-31 and Mon 2024-11-04).
        bars = _session_minutes(date(2024, 10, 31)) + _session_minutes(date(2024, 11, 4))
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        abstained = dict(verdict.abstained)
        assert abstained["muhurat"] == 1
        assert abstained["non_trading"] == 2  # the weekend

    def test_forming_session_abstains(self) -> None:
        # today_ist == the interior date → forming, never judged.
        verdict = classify_session_density(_bracketed([]), today_ist=_WED)
        assert verdict.flags == ()
        abstained = dict(verdict.abstained)
        assert abstained["forming_session"] == 2  # Wed (== today) + Thu (> today)
        # Tue is the first covered date → boundary.
        assert abstained["boundary"] == 1

    def test_out_of_coverage_abstains_first(self) -> None:
        # 2027 is outside the published holiday span — every walked date
        # abstains out_of_coverage BEFORE any trading-day call.
        bars = [_minute_ts(date(2027, 1, 4), 12, 0), _minute_ts(date(2027, 1, 6), 12, 0)]
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert verdict.abstained == (("out_of_coverage", 3),)
        assert verdict.sessions_checked == 0

    def test_default_today_reads_live_clock(self) -> None:
        # Omitting today_ist reads now_ist() — 2024 fixtures stay judgeable.
        verdict = classify_session_density(_bracketed(_session_minutes(_WED)))
        assert verdict.sessions_checked == 1

    def test_abstains_partition_the_walk(self) -> None:
        # judged + abstained == every date walked (first → last inclusive).
        bars = _session_minutes(date(2024, 1, 5)) + _session_minutes(date(2024, 1, 10))
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        walked = (date(2024, 1, 10) - date(2024, 1, 5)).days + 1
        assert verdict.sessions_checked + sum(n for _, n in verdict.abstained) == walked


class TestDatetimeBoundaryTotality:
    """Review round-3: the classifier is TOTAL at the ``datetime`` boundary.

    Both raise sites are corrupt-lake-only (a stored stamp at/near
    ``datetime.max``), but repo precedent (#1594 round 18) treats stored
    out-of-range datetimes as an in-scope hostile class — and the unguarded
    sweep-preflight consumer would have turned either raise into a
    refusal-by-crash at launch. Fails before the round-3 fix (both arms
    raised ``OverflowError``).
    """

    def test_date_max_bar_never_overflows_the_span_walk(self) -> None:
        # An IST stamp ON date.max previously overflowed the span walk's
        # `cursor += timedelta(days=1)`; the ordinal walk is total. date.max
        # (year 9999) is out of holiday coverage → the out_of_coverage
        # abstain, never a raise.
        stamp = datetime(9999, 12, 31, 12, 0, tzinfo=_IST)
        verdict = classify_session_density([stamp], today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert verdict.abstained == (("out_of_coverage", 1),)
        assert verdict.stamps_out_of_range == 0

    def test_near_datetime_max_utc_stamp_skips_counted(self) -> None:
        # A UTC stamp within 5:30 of datetime.max previously raised
        # OverflowError at `ts.astimezone(IST)`; it now folds into the
        # COUNTED stamps_out_of_range skip (never silently dropped).
        stamp = datetime(9999, 12, 31, 23, 0, tzinfo=UTC)
        verdict = classify_session_density([stamp], today_ist=_FAR_FUTURE)
        assert verdict.stamps_out_of_range == 1
        assert verdict.sessions_checked == 0
        assert verdict.flags == ()

    def test_out_of_range_stamp_never_disturbs_a_valid_window(self) -> None:
        # One poison stamp beside a real holey window: the SAME flag verdict
        # as without it, plus the honest count — the skip is surgical.
        hole = set(range(600, 645))
        bars = _bracketed(_session_minutes(_WED, skip=hole))
        clean = classify_session_density(bars, today_ist=_FAR_FUTURE)
        poisoned = classify_session_density(
            [*bars, datetime(9999, 12, 31, 23, 0, tzinfo=UTC)], today_ist=_FAR_FUTURE
        )
        assert poisoned.flags == clean.flags
        assert poisoned.stamps_out_of_range == 1
        assert clean.stamps_out_of_range == 0


class TestKnownIrregularArm:
    def test_extended_day_deficit_routes_to_info_arm(self) -> None:
        # The standing 2021-02-24 375/465 line: a full pre-fix 375-bar day on
        # the extended 465-minute session is DEFICIENT but KNOWN — INFO, never
        # WARN. (2021-02-23/25 bracket it.)
        bars = (
            _session_minutes(date(2021, 2, 23))
            + _session_minutes(date(2021, 2, 24))  # 375 of 465
            + _session_minutes(date(2021, 2, 25))
        )
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert len(verdict.known_irregular) == 1
        flag = verdict.known_irregular[0]
        assert (flag.expected, flag.observed) == (465, 375)
        assert flag.known_irregular is True
        assert flag.shape == SHAPE_INTRADAY_HOLE  # the missing 15:30-17:00 tail
        assert flag.largest_gap_minutes == 90

    def test_halt_day_deficit_routes_to_info_arm(self) -> None:
        # 2020-03-13 (halt 09:20→10:20): the ~59 missing halt-window minutes
        # are a documented market fact — INFO, never WARN. Bracketed by
        # 2020-03-12 (Thu) + 2020-03-16 (Mon).
        halt_window = set(range(9 * 60 + 20, 10 * 60 + 20))
        bars = (
            _session_minutes(date(2020, 3, 12))
            + _session_minutes(date(2020, 3, 13), skip=halt_window)
            + _session_minutes(date(2020, 3, 16))
        )
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert len(verdict.known_irregular) == 1
        assert verdict.known_irregular[0].session == date(2020, 3, 13)

    def test_healthy_extended_day_never_flags(self) -> None:
        # A POST-re-pull 465-bar extended day is dense — nothing fires.
        bars = (
            _session_minutes(date(2021, 2, 23))
            + _session_minutes(date(2021, 2, 24), count=465)
            + _session_minutes(date(2021, 2, 25))
        )
        verdict = classify_session_density(bars, today_ist=_FAR_FUTURE)
        assert verdict.flags == ()
        assert verdict.known_irregular == ()


class _Log:
    """A duck-typed structlog stand-in recording (level, event, fields)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.records.append(("warning", event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.records.append(("info", event, fields))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [f for _, e, f in self.records if e == name]


class TestObserveSessionDensity:
    def test_flag_emits_one_capped_warn_with_remediation(self) -> None:
        log = _Log()
        hole = set(range(600, 645))
        verdict = observe_session_density(
            log,
            bar_timestamps=_bracketed(_session_minutes(_WED, skip=hole)),
            symbol="NIFTY",
            interval="1minute",
            pull_id="pid-1",
            today_ist=_FAR_FUTURE,
        )
        assert verdict is not None
        warns = log.events(SESSION_DENSITY_LOW_EVENT)
        assert len(warns) == 1
        fields = warns[0]
        assert fields["symbol"] == repr("NIFTY")
        assert fields["session"] == "2024-01-03"
        assert (fields["expected"], fields["observed"]) == (375, 330)
        assert fields["shape"] == SHAPE_INTRADAY_HOLE
        assert (fields["gap_start"], fields["gap_end"]) == ("10:00", "10:45")
        assert "re-pull this window" in fields["remediation"]
        assert log.events(SESSION_DENSITY_SUPPRESSED_EVENT) == []

    def test_sparse_flag_carries_the_inspect_remediation(self) -> None:
        log = _Log()
        scattered = set(range(600, 680, 2))
        observe_session_density(
            log,
            bar_timestamps=_bracketed(_session_minutes(_WED, skip=scattered)),
            symbol="NIFTY",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        (fields,) = log.events(SESSION_DENSITY_LOW_EVENT)
        assert fields["shape"] == SHAPE_SPARSE
        assert "vendor 1m depth" in fields["remediation"]

    def test_known_extended_day_emits_info_with_repull_pointer(self) -> None:
        log = _Log()
        bars = (
            _session_minutes(date(2021, 2, 23))
            + _session_minutes(date(2021, 2, 24))
            + _session_minutes(date(2021, 2, 25))
        )
        observe_session_density(
            log,
            bar_timestamps=bars,
            symbol="NIFTY",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        assert log.events(SESSION_DENSITY_LOW_EVENT) == []
        infos = log.events(SESSION_DENSITY_KNOWN_EVENT)
        assert len(infos) == 1
        assert infos[0]["reason"] == "exchange_outage_extended_close"
        assert "re-pull this window" in infos[0]["remediation"]
        assert ("info", SESSION_DENSITY_KNOWN_EVENT, infos[0]) in log.records

    def test_known_halt_day_reads_no_action_needed(self) -> None:
        log = _Log()
        halt_window = set(range(9 * 60 + 58, 10 * 60 + 58))
        bars = (
            _session_minutes(date(2020, 3, 20))  # Friday before
            + _session_minutes(date(2020, 3, 23), skip=halt_window)
            + _session_minutes(date(2020, 3, 24))
        )
        observe_session_density(
            log,
            bar_timestamps=bars,
            symbol="SENSEX",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        (info,) = log.events(SESSION_DENSITY_KNOWN_EVENT)
        assert info["reason"] == "market_wide_circuit_halt"
        assert "no action needed" in info["remediation"]

    def test_warn_cap_plus_suppressed_summary(self) -> None:
        # >20 hollow interior sessions (the real Jan-Feb 2024 trading days —
        # calendar-derived, never guessed) → exactly 20 WARNs + ONE suppressed
        # summary carrying the TRUE count.
        log = _Log()
        span = [date(2024, 1, 2) + timedelta(days=i) for i in range(45)]
        trading = [d for d in span if is_trading_day(d)]
        bars = _session_minutes(trading[0])
        for d in trading[1:-1]:
            bars.append(_minute_ts(d, 9, 15))  # one hollow bar per interior day
        bars += _session_minutes(trading[-1])
        observe_session_density(
            log,
            bar_timestamps=bars,
            symbol="NIFTY",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        warns = log.events(SESSION_DENSITY_LOW_EVENT)
        assert len(warns) == SESSION_DENSITY_WARN_CAP
        (summary,) = log.events(SESSION_DENSITY_SUPPRESSED_EVENT)
        assert summary["arm"] == "low"
        assert summary["cap"] == SESSION_DENSITY_WARN_CAP
        assert summary["total"] == len(trading) - 2  # every interior day flagged
        assert summary["total"] > SESSION_DENSITY_WARN_CAP

    def test_exactly_cap_flags_emit_all_warns_and_no_false_suppressed(self) -> None:
        # Review round-3 mutant killer (`>` -> `>=` at the suppressed gate):
        # EXACTLY cap flagged sessions emit exactly cap WARN lines and NO
        # suppressed summary — the mutant emits a lying {cap: 20, total: 20}
        # "suppression" line when nothing was suppressed (Golden Rule #1: the
        # cap never lies). 22 real trading days -> 20 flagged interiors.
        log = _Log()
        trading: list[date] = []
        d = date(2024, 1, 2)
        while len(trading) < SESSION_DENSITY_WARN_CAP + 2:
            if is_trading_day(d):
                trading.append(d)
            d += timedelta(days=1)
        bars = _session_minutes(trading[0])
        for t in trading[1:-1]:
            bars.append(_minute_ts(t, 9, 15))  # one hollow bar per interior day
        bars += _session_minutes(trading[-1])
        observe_session_density(
            log,
            bar_timestamps=bars,
            symbol="NIFTY",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        warns = log.events(SESSION_DENSITY_LOW_EVENT)
        assert len(warns) == SESSION_DENSITY_WARN_CAP  # every flag emitted
        assert log.events(SESSION_DENSITY_SUPPRESSED_EVENT) == []

    def test_kill_switch_disables_with_zero_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSION_DENSITY_KILL_ENV, "0")
        calls = {"n": 0}

        def _boom(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            raise AssertionError("classifier must not run under the kill switch")

        monkeypatch.setattr(sd, "classify_session_density", _boom)
        log = _Log()
        assert (
            observe_session_density(log, bar_timestamps=[], symbol="NIFTY", interval="1minute")
            is None
        )
        assert calls["n"] == 0
        assert log.records == []

    def test_error_guard_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected census crash")

        monkeypatch.setattr(sd, "classify_session_density", _boom)
        log = _Log()
        got = observe_session_density(
            log,
            bar_timestamps=[_minute_ts(_TUE, 12, 0)],
            symbol="NIFTY",
            interval="1minute",
            pull_id="pid-9",
        )
        assert got is None
        (err,) = log.events(SESSION_DENSITY_ERROR_EVENT)
        assert err["pull_id"] == "pid-9"
        assert "injected census crash" in err["error"]

    def test_healthy_window_emits_nothing(self) -> None:
        log = _Log()
        verdict = observe_session_density(
            log,
            bar_timestamps=_bracketed(_session_minutes(_WED)),
            symbol="NIFTY",
            interval="1minute",
            today_ist=_FAR_FUTURE,
        )
        assert verdict is not None
        assert log.records == []

    def test_structlog_capture_smoke(self) -> None:
        # The duck-typed contract holds against the REAL structlog logger too.
        import structlog

        with capture_logs() as logs:
            observe_session_density(
                structlog.get_logger("test"),
                bar_timestamps=_bracketed([]),
                symbol="NIFTY",
                interval="1minute",
                today_ist=_FAR_FUTURE,
            )
        assert [e["event"] for e in logs] == [SESSION_DENSITY_LOW_EVENT]

    def test_emit_arm_tolerates_none_gap_bounds(self) -> None:
        # The frozen record ADMITS None gap bounds (the dataclass contract);
        # the emitter renders them as honest nulls, never a crash.
        flag = SessionDensityFlag(
            session=_WED,
            expected=375,
            observed=300,
            fraction=0.8,
            largest_gap_minutes=0,
            gap_start_ist=None,
            gap_end_ist=None,
            shape=SHAPE_SPARSE,
            known_irregular=False,
        )
        log = _Log()
        sd._emit_arm(  # pyright: ignore[reportPrivateUsage]
            log,
            [flag],
            event=SESSION_DENSITY_LOW_EVENT,
            arm="low",
            info=False,
            symbol="NIFTY",
            interval="1minute",
            pull_id=None,
        )
        (fields,) = log.events(SESSION_DENSITY_LOW_EVENT)
        assert fields["gap_start"] is None
        assert fields["gap_end"] is None

"""Unit tests for the signal→next-1m execution-grid mapping (`brutex.engine.exec_grid`).

100% line + branch coverage of the DEC-FILL-EXEC-GRID-1M-001 mapping leaf
(design ``docs/design/fills/PRB_1M_EXECUTION_GRID_DESIGN.md`` §1.2/§2). Covers:

* the ``>=``-close-instant mapping law (exact-instant fill vs the SLID fill on
  a hole — pure UTC arithmetic, every expected column hand-derived in-line);
* ``decision_of`` forward ownership (``-1`` before the first decision label);
* the B4 earliest-armed-wins collision law + ``collision_losers``;
* the session-bounded slide (cross-session candidates DROP, no arm);
* the B6 bounded-distance refusal + the named
  ``exec_grid.fill_selector_distance_refused`` WARN;
* the ``_BUILD_WARN_CAP`` capped-WARN law (per-index WARNs + the one
  ``*_suppressed`` summary) on the distance and slid families;
* ``_require_chronological`` (decreasing refuses on EITHER stream; duplicate
  timestamps tolerated — the ``>=`` law picks the FIRST duplicate);
* the ``_session_close_instant`` arms (regular 15:30 · Muhurat session close ·
  in-coverage non-session fallback · out-of-coverage weekday LOUD raise · the
  per-day memo);
* every ``ExecGridMap.__post_init__`` validation arm;
* the tf=1 identity shape and the empty-stream degenerates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import (
    ALL_HOLIDAYS,
    IST,
    MUHURAT_SESSIONS,
    HolidayDataUnavailableError,
)
from brutex.data.models import CandleBar
from brutex.engine import exec_grid as eg_mod
from brutex.engine.exec_grid import ExecGridMap, build_exec_grid_map
from structlog.testing import capture_logs

_DAY = date(2024, 1, 2)  # a regular NSE trading Tuesday (session close 15:30 IST)


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    """UTC-aware timestamp for an IST wall-clock ``hh:mm`` (bars store UTC)."""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, *, day: date = _DAY) -> CandleBar:
    """A flat doji bar (O=H=L=C=100.00) at IST ``hh:mm`` — the mapping reads only ts."""
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


def _bars(*hhmm: tuple[int, int], day: date = _DAY) -> list[CandleBar]:
    """Build a bar list from ``(hh, mm)`` IST wall-clock pairs on one day."""
    return [_bar(hh, mm, day=day) for hh, mm in hhmm]


def _events(logs: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == event]


# ────────────────────────────────────────────────────────────────────────
# The mapping law (§1.2)
# ────────────────────────────────────────────────────────────────────────


class TestMappingLaw:
    def test_tf1_identity_contiguous_same_stream(self) -> None:
        """tf=1 identity: same contiguous stream both sides → arm_source[N+1] = N.

        Bars 09:15..09:19. Decision N's close instant is label + 1min, so
        exec_idx(N) = N+1 exactly (arm_slid False everywhere):
            arm_source  = (-1, 0, 1, 2, 3)   # bar 09:15 precedes every close
            decision_of = (0, 1, 2, 3, 4)    # label j <= ts_1m[j], forward
        The last decision bar (09:19, close 09:20) has NO stored candidate —
        the tail break (design §5.2 cause 4).
        """
        bars = _bars((9, 15), (9, 16), (9, 17), (9, 18), (9, 19))
        grid = build_exec_grid_map(bars, bars, tf_minutes=1)
        assert grid.arm_source == (-1, 0, 1, 2, 3)
        assert grid.decision_of == (0, 1, 2, 3, 4)
        assert grid.arm_slid == (False, False, False, False, False)
        assert grid.n_decision == 5
        assert grid.n_exec == 5
        assert grid.collision_losers == ()
        assert grid.distance_refused == ()
        assert grid.cross_session_dropped == ()

    def test_tf5_exact_close_instant_fill_not_slid(self) -> None:
        """tf=5: the fill is the 1m bar labelled EXACTLY at label+5min (not slid).

        Decision labels 09:15 and 09:20; exec = the 12 contiguous 1m bars
        09:15..09:26 (indices 0..11).
            exec_idx(0): close 09:20 → 1m index 5 (exact → arm_slid False)
            exec_idx(1): close 09:25 → 1m index 10 (exact → arm_slid False)
        decision_of: j 0..4 (09:15..09:19) → 0; j 5..11 (09:20..09:26) → 1
        (the 09:20 label <= ts from j=5 on).
        """
        decision = _bars((9, 15), (9, 20))
        execs = _bars(*[(9, 15 + i) for i in range(12)])
        grid = build_exec_grid_map(decision, execs, tf_minutes=5)
        expected_arm = [-1] * 12
        expected_arm[5] = 0
        expected_arm[10] = 1
        assert grid.arm_source == tuple(expected_arm)
        assert grid.decision_of == (0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1)
        assert grid.arm_slid == (False,) * 12
        assert grid.collision_losers == ()

    def test_slid_fill_on_hole_first_stored_after_close_instant(self) -> None:
        """A hole at the close instant slides to the next STORED minute (arm_slid).

        tf=1, decision [09:15, 09:16]; exec [09:15, 09:16, 09:19]:
            exec_idx(0): close 09:16 → 1m index 1 exactly (not slid)
            exec_idx(1): close 09:17 → first stored >= is 09:19 (index 2) →
                         SLID (fill opens strictly after the close instant)
        arm_source = (-1, 0, 1); arm_slid = (F, F, T); one
        ``exec_grid.fill_slid_minute`` WARN names (exec_idx=2, decision_idx=1).
        """
        decision = _bars((9, 15), (9, 16))
        execs = _bars((9, 15), (9, 16), (9, 19))
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1, 0, 1)
        assert grid.arm_slid == (False, False, True)
        slid = _events(logs, "exec_grid.fill_slid_minute")
        assert len(slid) == 1
        assert slid[0]["exec_idx"] == 2
        assert slid[0]["decision_idx"] == 1
        assert _events(logs, "exec_grid.fill_slid_minute_suppressed") == []

    def test_decision_of_minus_one_before_first_decision_label(self) -> None:
        """An exec bar BEFORE the first decision label owns no bucket (-1).

        Decision label 09:15; exec [09:14, 09:15, 09:16]:
            decision_of = (-1, 0, 0); exec_idx(0): close 09:16 → index 2.
        """
        decision = _bars((9, 15))
        execs = _bars((9, 14), (9, 15), (9, 16))
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.decision_of == (-1, 0, 0)
        assert grid.arm_source == (-1, -1, 0)
        assert grid.arm_slid == (False, False, False)

    def test_tail_break_when_no_stored_candidate_remains(self) -> None:
        """A close instant past the stored tail arms nothing (the §5.2 break).

        Decision [09:15, 09:16]; exec = [09:15] only. Decision 0's close
        instant 09:16 exhausts the exec stream → break; decision 1 is never
        considered (no drop/refusal accounting — the entries simply never
        fill).
        """
        decision = _bars((9, 15), (9, 16))
        execs = _bars((9, 15))
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1,)
        assert grid.arm_slid == (False,)
        assert grid.decision_of == (0,)
        assert grid.collision_losers == ()
        assert grid.distance_refused == ()
        assert grid.cross_session_dropped == ()

    def test_empty_exec_stream(self) -> None:
        """No exec bars: every column is empty; nothing arms, nothing raises."""
        decision = _bars((9, 15), (9, 16))
        grid = build_exec_grid_map(decision, [], tf_minutes=1)
        assert grid.n_decision == 2
        assert grid.n_exec == 0
        assert grid.arm_source == ()
        assert grid.decision_of == ()
        assert grid.arm_slid == ()

    def test_empty_decision_stream(self) -> None:
        """No decision bars: every exec bar is armless and owner-less (-1)."""
        execs = _bars((9, 15), (9, 16))
        grid = build_exec_grid_map([], execs, tf_minutes=1)
        assert grid.n_decision == 0
        assert grid.n_exec == 2
        assert grid.arm_source == (-1, -1)
        assert grid.decision_of == (-1, -1)
        assert grid.arm_slid == (False, False)

    def test_duplicate_exec_timestamps_first_duplicate_wins(self) -> None:
        """The ``>=`` law deterministically selects the FIRST duplicate.

        Decision [09:15]; exec [09:15, 09:16, 09:16(dup), 09:17]: close 09:16
        → index 1 (the first 09:16), not index 2.
        """
        decision = _bars((9, 15))
        execs = [_bar(9, 15), _bar(9, 16), _bar(9, 16), _bar(9, 17)]
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1, 0, -1, -1)
        assert grid.arm_slid == (False, False, False, False)

    def test_tf_minutes_below_one_refused(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1, got 0"):
            build_exec_grid_map([], [], tf_minutes=0)


# ────────────────────────────────────────────────────────────────────────
# B4 collision — earliest-armed wins
# ────────────────────────────────────────────────────────────────────────


class TestCollisionWinnerLaw:
    def test_earliest_armed_wins_loser_recorded_and_warned(self) -> None:
        """Two decision bars mapping onto ONE 1m bar: the earlier wins.

        tf=1, decision [09:15, 09:16]; exec [09:15, 09:18]:
            exec_idx(0): close 09:16 → first stored >= is 09:18 (index 1, SLID)
            exec_idx(1): close 09:17 → also 09:18 — but index 1 is already
                         claimed by decision 0 → decision 1 LOSES (B4).
        arm_source = (-1, 0); collision_losers = (1,); one
        ``exec_grid.signals_dropped_collision`` WARN with decision_idx=1.
        """
        decision = _bars((9, 15), (9, 16))
        execs = _bars((9, 15), (9, 18))
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1, 0)
        assert grid.arm_slid == (False, True)
        assert grid.collision_losers == (1,)
        collision = _events(logs, "exec_grid.signals_dropped_collision")
        assert len(collision) == 1
        assert collision[0]["decision_idx"] == 1
        assert _events(logs, "exec_grid.signals_dropped_collision_suppressed") == []


# ────────────────────────────────────────────────────────────────────────
# Session-bounded slide — cross-session DROP (§2.9)
# ────────────────────────────────────────────────────────────────────────


class TestCrossSessionDrop:
    def test_candidate_on_next_session_drops_without_arming(self) -> None:
        """A fill candidate on a LATER IST session date DROPS (no arm, no warn).

        Day-1 (2024-01-02) decision [15:28, 15:29]; exec = day-1 [15:28,
        15:29] + day-2 (2024-01-03) [09:15, 09:16]:
            exec_idx(0): close 15:29 → day-1 index 1 (same session, < 15:30
                         close → ARMED, exact instant)
            exec_idx(1): close 15:30 → first stored >= is day-2 09:15 → a
                         DIFFERENT IST date → cross_session_dropped.
        arm_source = (-1, 0, -1, -1); decision_of = (0, 1, 1, 1).
        """
        day2 = date(2024, 1, 3)
        decision = _bars((15, 28), (15, 29))
        execs = _bars((15, 28), (15, 29)) + _bars((9, 15), (9, 16), day=day2)
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1, 0, -1, -1)
        assert grid.decision_of == (0, 1, 1, 1)
        assert grid.cross_session_dropped == (1,)
        assert grid.collision_losers == ()
        assert grid.distance_refused == ()
        # The cross-session DROP has no per-index WARN family of its own.
        assert _events(logs, "exec_grid.fill_selector_distance_refused") == []


# ────────────────────────────────────────────────────────────────────────
# B6 bounded-distance refusal
# ────────────────────────────────────────────────────────────────────────


class TestDistanceRefusal:
    def test_same_day_candidate_at_session_close_refused(self) -> None:
        """A same-IST-date candidate at/after the session close REFUSES (B6).

        Decision [15:29] (regular Tuesday, close 15:30); exec = a stored
        15:30 bar on the SAME day. Close instant 15:30 → candidate 15:30 >=
        the session close instant 15:30 → ``distance_refused`` — never a
        fill; the named ``exec_grid.fill_selector_distance_refused`` WARN.
        """
        decision = _bars((15, 29))
        execs = _bars((15, 30))
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (-1,)
        assert grid.distance_refused == (0,)
        assert grid.cross_session_dropped == ()
        refused = _events(logs, "exec_grid.fill_selector_distance_refused")
        assert len(refused) == 1
        assert refused[0]["decision_idx"] == 0
        assert _events(logs, "exec_grid.fill_selector_distance_refused_suppressed") == []


# ────────────────────────────────────────────────────────────────────────
# Capped WARNs (_BUILD_WARN_CAP = 20)
# ────────────────────────────────────────────────────────────────────────


class TestCappedWarns:
    def test_distance_refused_past_cap_rolls_into_one_summary(self) -> None:
        """25 refusals → exactly 20 per-index WARNs + ONE suppressed summary.

        25 DUPLICATE decision bars at 15:29 (duplicates are tolerated) all
        map to the same stored 15:30 bar — each refused by B6 (the j pointer
        never advances past the shared candidate).
        """
        decision = [_bar(15, 29) for _ in range(25)]
        execs = _bars((15, 30))
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.distance_refused == tuple(range(25))
        refused = _events(logs, "exec_grid.fill_selector_distance_refused")
        assert len(refused) == eg_mod._BUILD_WARN_CAP == 20
        assert [entry["decision_idx"] for entry in refused] == list(range(20))
        summary = _events(logs, "exec_grid.fill_selector_distance_refused_suppressed")
        assert len(summary) == 1
        assert summary[0]["total"] == 25
        assert summary[0]["shown"] == 20

    def test_distance_refused_exactly_at_cap_no_summary(self) -> None:
        """Exactly 20 refusals: 20 WARNs, NO suppressed summary (boundary)."""
        decision = [_bar(15, 29) for _ in range(20)]
        execs = _bars((15, 30))
        with capture_logs() as logs:
            grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.distance_refused == tuple(range(20))
        assert len(_events(logs, "exec_grid.fill_selector_distance_refused")) == 20
        assert _events(logs, "exec_grid.fill_selector_distance_refused_suppressed") == []

    def test_slid_fills_past_cap_roll_into_one_summary(self) -> None:
        """21 slid fills → 20 ``fill_slid_minute`` WARNs + ONE suppressed summary.

        22 alternate-minute bars (09:15, 09:17, .., minute 555+2i), the SAME
        stream both sides, tf=1. Decision N's close instant is minute
        555+2N+1 — never stored — so the fill slides to bar N+1 at minute
        555+2N+2: arm_source[N+1] = N (SLID) for N = 0..20 → 21 slid bars.
        The last decision bar (N=21, close minute 599) exhausts the stored
        tail → break.
        """
        bars = [_bar(*divmod(555 + 2 * i, 60)) for i in range(22)]
        with capture_logs() as logs:
            grid = build_exec_grid_map(bars, bars, tf_minutes=1)
        assert grid.arm_source == (-1, *range(21))
        assert grid.arm_slid == (False,) + (True,) * 21
        slid = _events(logs, "exec_grid.fill_slid_minute")
        assert len(slid) == 20
        assert [entry["exec_idx"] for entry in slid] == list(range(1, 21))
        summary = _events(logs, "exec_grid.fill_slid_minute_suppressed")
        assert len(summary) == 1
        assert summary[0]["total"] == 21
        assert summary[0]["shown"] == 20


# ────────────────────────────────────────────────────────────────────────
# _require_chronological — the two-pointer precondition
# ────────────────────────────────────────────────────────────────────────


class TestChronologicalRefusal:
    def test_decreasing_decision_stream_refused(self) -> None:
        decision = [_bar(9, 16), _bar(9, 15)]
        with pytest.raises(ValueError, match="decision_bars timestamps decrease at index 1"):
            build_exec_grid_map(decision, _bars((9, 15)), tf_minutes=1)

    def test_decreasing_exec_stream_refused(self) -> None:
        execs = [_bar(9, 15), _bar(9, 17), _bar(9, 16)]
        with pytest.raises(ValueError, match="exec_bars_1m timestamps decrease at index 2"):
            build_exec_grid_map(_bars((9, 15)), execs, tf_minutes=1)

    def test_duplicate_timestamps_tolerated_on_both_streams(self) -> None:
        """Duplicates are NOT a refusal on either stream (ordered-but-equal)."""
        decision = [_bar(9, 15), _bar(9, 15)]
        execs = [_bar(9, 16), _bar(9, 16)]
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        # Both decision bars close at 09:16 → the FIRST duplicate exec bar is
        # the shared candidate: decision 0 wins it, decision 1 collides (B4).
        assert grid.arm_source == (0, -1)
        assert grid.collision_losers == (1,)


# ────────────────────────────────────────────────────────────────────────
# _session_close_instant — the B6 bound's calendar arms
# ────────────────────────────────────────────────────────────────────────


class TestSessionCloseArms:
    def test_muhurat_session_close_governs_the_bound(self) -> None:
        """A Muhurat day's OWN close (19:00) is the B6 bound, not 15:30.

        2024-11-01 Muhurat session 18:00-19:00 (pinned from the canonical
        MUHURAT_SESSIONS map). Decision [18:00, 18:58, 18:59]; exec [18:01,
        18:59, 19:00]:
            exec_idx(0): close 18:01 → armed at index 0 (18:01 < 19:00; under
                         a wrongly-applied 15:30 close this would refuse —
                         the arm itself pins the session-specific bound)
            exec_idx(1): close 18:59 → armed at index 1 (exact)
            exec_idx(2): close 19:00 → candidate 19:00 same day >= the
                         Muhurat close 19:00 → distance_refused.
        """
        muhurat_day = date(2024, 11, 1)
        assert MUHURAT_SESSIONS[muhurat_day][1].hour == 19  # the pinned close
        decision = _bars((18, 0), (18, 58), (18, 59), day=muhurat_day)
        execs = _bars((18, 1), (18, 59), (19, 0), day=muhurat_day)
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (0, 1, -1)
        assert grid.distance_refused == (2,)

    def test_in_coverage_weekend_falls_back_to_regular_close(self) -> None:
        """A stored weekend date (no session) uses the 15:30 fallback bound.

        Saturday 2024-01-06: get_session_checked → None (data-complete) →
        SESSION_CLOSE_IST 15:30. A 10:00→10:01 fill ARMS; a 15:29 decision
        bar's 15:30 candidate REFUSES — proving the fallback bound applied.
        """
        saturday = date(2024, 1, 6)
        assert saturday.weekday() == 5
        decision = _bars((10, 0), (15, 29), day=saturday)
        execs = _bars((10, 1), (15, 30), day=saturday)
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (0, -1)
        assert grid.distance_refused == (1,)

    def test_in_coverage_holiday_falls_back_to_regular_close(self) -> None:
        """An in-coverage weekday HOLIDAY (no session) uses the 15:30 fallback."""
        holiday = date(2024, 1, 26)  # Republic Day 2024 — a Friday holiday
        assert holiday in ALL_HOLIDAYS
        decision = _bars((10, 0), (15, 29), day=holiday)
        execs = _bars((10, 1), (15, 30), day=holiday)
        grid = build_exec_grid_map(decision, execs, tf_minutes=1)
        assert grid.arm_source == (0, -1)
        assert grid.distance_refused == (1,)

    def test_out_of_coverage_weekday_raises_loudly(self) -> None:
        """A weekday past the published holiday span REFUSES (never a guess)."""
        far = date(2027, 1, 4)  # a Monday beyond HOLIDAY_COVERAGE_LAST_YEAR
        assert far.weekday() == 0
        decision = _bars((10, 0), day=far)
        execs = _bars((10, 1), day=far)
        with pytest.raises(HolidayDataUnavailableError):
            build_exec_grid_map(decision, execs, tf_minutes=1)

    def test_close_instant_memoised_per_day(self) -> None:
        """One calendar resolution per IST day (the per-day cache arm)."""
        calls: list[date] = []
        real = eg_mod.get_session_checked

        def _counting(d: date) -> object:
            calls.append(d)
            return real(d)

        # Two same-day decision bars both reach the B6 check → ONE resolution.
        decision = _bars((9, 15), (9, 16), (9, 17))
        original = eg_mod.get_session_checked
        eg_mod.get_session_checked = _counting  # type: ignore[assignment]
        try:
            grid = build_exec_grid_map(decision, decision, tf_minutes=1)
        finally:
            eg_mod.get_session_checked = original  # type: ignore[assignment]
        assert grid.arm_source == (-1, 0, 1)
        assert calls == [_DAY]


# ────────────────────────────────────────────────────────────────────────
# ExecGridMap.__post_init__ validation
# ────────────────────────────────────────────────────────────────────────

# A consistent hand-built baseline: n_decision=2, n_exec=3.
_ARM = (-1, 0, 1)
_OWN = (0, 1, 1)
_SLID = (False, False, True)


def _map(**overrides: object) -> ExecGridMap:
    kwargs: dict[str, object] = {
        "arm_source": _ARM,
        "decision_of": _OWN,
        "n_decision": 2,
        "n_exec": 3,
        "arm_slid": _SLID,
    }
    kwargs.update(overrides)
    return ExecGridMap(**kwargs)  # type: ignore[arg-type]


class TestExecGridMapValidation:
    def test_valid_hand_built_map_constructs(self) -> None:
        grid = _map(collision_losers=(1,), distance_refused=(0,), cross_session_dropped=(1,))
        assert grid.n_decision == 2
        assert grid.arm_source == _ARM

    def test_negative_n_decision_refused(self) -> None:
        with pytest.raises(ValueError, match="negative stream length"):
            _map(n_decision=-1)

    def test_negative_n_exec_refused(self) -> None:
        with pytest.raises(ValueError, match="negative stream length"):
            _map(n_exec=-3)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("arm_source", (-1, 0)),
            ("decision_of", (0, 1)),
            ("arm_slid", (False, False)),
        ],
    )
    def test_column_length_mismatch_refused(self, field: str, value: tuple[object, ...]) -> None:
        with pytest.raises(ValueError, match=f"{field} length 2 != n_exec 3"):
            _map(**{field: value})

    @pytest.mark.parametrize("bad", [-2, 2])
    def test_arm_source_out_of_range_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match=r"arm_source\[1\] = .* outside \[-1, 2\)"):
            _map(arm_source=(-1, bad, -1), arm_slid=(False, False, False))

    @pytest.mark.parametrize("pair", [(1, 0), (1, 1)])
    def test_arm_source_not_strictly_increasing_refused(self, pair: tuple[int, int]) -> None:
        first, second = pair
        with pytest.raises(ValueError, match="arm_source not strictly increasing"):
            _map(arm_source=(first, second, -1), arm_slid=(False, False, False))

    def test_arm_slid_on_armless_bar_refused(self) -> None:
        with pytest.raises(ValueError, match=r"arm_slid\[0\] set on an armless bar"):
            _map(arm_slid=(True, False, False))

    @pytest.mark.parametrize("bad", [-2, 2])
    def test_decision_of_out_of_range_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match=r"decision_of\[2\] = .* outside \[-1, 2\)"):
            _map(decision_of=(0, 1, bad))

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("collision_losers", -1),
            ("distance_refused", 2),
            ("cross_session_dropped", 7),
        ],
    )
    def test_accounting_indices_out_of_range_refused(self, field: str, bad: int) -> None:
        with pytest.raises(ValueError, match=f"{field} entry {bad} outside"):
            _map(**{field: (bad,)})

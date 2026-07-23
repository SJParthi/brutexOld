"""Unit + property tests for the pure sweep-scheduler core (`brutex.engine.sweep_scheduler`).

100% line + branch coverage of the clock-injected scheduler brain
(DEC-SWEEP-SCHEDULER-001, CLAUDE.md §11 AUTOMATED):

* :meth:`Schedule.parse` — every accepted form (interval ``s/m/h/d``, ``@hourly``,
  daily ``HH:MM`` / ``daily@HH:MM`` / ``@daily``) and every reject branch (blank,
  zero interval, out-of-range / unparseable);
* :meth:`Schedule.next_fire_after` — interval (first loop, on-schedule, caught-up)
  and daily (today / tomorrow / re-fire guard, with + without ``last_fire``);
* :meth:`Schedule.seconds_until` — positive + clamped-to-zero;
* :meth:`CheckpointView.from_loaded` — ``None`` / matching / mismatching;
* :func:`decide_resume` — FRESH (no ckpt / wrong id) · ALREADY_COMPLETE · RESUME,
  plus :attr:`ResumeDecision.should_run`;
* :func:`plan_cycle` — ``run_immediately`` + scheduled (positive + zero sleep);
* :func:`cycle_audit_payload` — the exact JSON-native shape;
* hypothesis properties: ``next_fire_after`` is monotone-safe + never re-fires,
  and the daily fire always lands on the configured wall-clock second.
"""

from __future__ import annotations

import math

import pytest
from brutex.engine.sweep_checkpoint import SweepCheckpoint
from brutex.engine.sweep_scheduler import (
    IST_OFFSET_SECONDS,
    CheckpointView,
    CyclePlan,
    ResumeKind,
    Schedule,
    ScheduleKind,
    SchedulerError,
    cycle_audit_payload,
    decide_resume,
    plan_cycle,
)
from hypothesis import given
from hypothesis import strategies as st

_DAY = 86_400


# ────────────────────────────────────────────────────────────────────────
# Schedule.parse — accept + reject branches
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "seconds"),
    [
        ("30s", 30),
        ("90m", 5_400),
        ("24h", 86_400),
        ("1d", 86_400),
        ("@hourly", 3_600),
        (" 2H ", 7_200),  # surrounding whitespace + upper-case normalised
    ],
)
def test_parse_interval_forms(spec: str, seconds: int) -> None:
    s = Schedule.parse(spec)
    assert s.kind is ScheduleKind.INTERVAL
    assert s.interval_seconds == seconds
    assert s.second_of_day == 0
    assert s.tz_offset_seconds == 0


@pytest.mark.parametrize(
    ("spec", "second_of_day"),
    [
        ("00:00", 0),
        ("@daily", 0),
        ("02:30", 2 * 3_600 + 30 * 60),
        ("daily@23:59", 23 * 3_600 + 59 * 60),
        ("15:00", 54_000),
    ],
)
def test_parse_daily_forms(spec: str, second_of_day: int) -> None:
    s = Schedule.parse(spec, tz_offset_seconds=IST_OFFSET_SECONDS)
    assert s.kind is ScheduleKind.DAILY
    assert s.second_of_day == second_of_day
    assert s.interval_seconds == 0
    assert s.tz_offset_seconds == IST_OFFSET_SECONDS


@pytest.mark.parametrize("spec", ["", "   ", "\t"])
def test_parse_blank_rejected(spec: str) -> None:
    with pytest.raises(SchedulerError, match="non-empty"):
        Schedule.parse(spec)


@pytest.mark.parametrize("spec", ["0h", "0s", "0m", "0d"])
def test_parse_zero_interval_rejected(spec: str) -> None:
    with pytest.raises(SchedulerError, match="> 0 seconds"):
        Schedule.parse(spec)


@pytest.mark.parametrize("spec", ["bogus", "5x", "24", "25:00", "12:60", "daily@99:99", "-1h"])
def test_parse_unrecognised_rejected(spec: str) -> None:
    with pytest.raises(SchedulerError, match="unrecognised"):
        Schedule.parse(spec)


# ────────────────────────────────────────────────────────────────────────
# next_fire_after — interval cadence
# ────────────────────────────────────────────────────────────────────────


def test_interval_first_loop_waits_one_interval() -> None:
    s = Schedule.parse("24h")
    # last_fire is None ⇒ anchor = now ⇒ first fire one interval out.
    assert s.next_fire_after(1_000.0) == 1_000.0 + _DAY


def test_interval_on_schedule_advances_from_last_fire() -> None:
    s = Schedule.parse("1h")
    # last_fire given, candidate (last+interval) still in the future ⇒ that.
    assert s.next_fire_after(1_000.0, last_fire=500.0) == 500.0 + 3_600


def test_interval_caught_up_fires_now() -> None:
    s = Schedule.parse("1h")
    # Daemon fell far behind ⇒ candidate <= now ⇒ fire immediately (== now).
    assert s.next_fire_after(10_000.0, last_fire=0.0) == 10_000.0


# ────────────────────────────────────────────────────────────────────────
# next_fire_after — daily cadence
# ────────────────────────────────────────────────────────────────────────


def test_daily_today_when_fire_still_ahead() -> None:
    s = Schedule.parse("02:30", tz_offset_seconds=IST_OFFSET_SECONDS)
    now = 0.0  # 1970-01-01 00:00 UTC == 05:30 IST; 02:30 IST already passed today?
    nf = s.next_fire_after(now)
    # The fire lands at 02:30 in the schedule's tz, at-or-after `now`.
    assert (nf + IST_OFFSET_SECONDS) % _DAY == s.second_of_day
    assert nf >= now


def test_daily_rolls_to_tomorrow_when_today_passed() -> None:
    s = Schedule.parse("00:00", tz_offset_seconds=0)  # midnight UTC
    nf_today = s.next_fire_after(0.0)
    assert nf_today == 0.0  # exactly midnight, at-or-after now
    # One second past midnight ⇒ today's fire is behind ⇒ next is tomorrow.
    assert s.next_fire_after(1.0) == float(_DAY)


def test_daily_refire_guard_rolls_forward() -> None:
    s = Schedule.parse("00:00", tz_offset_seconds=0)
    fired = s.next_fire_after(0.0)
    # Recomputing immediately after firing must not re-fire the same instant.
    assert s.next_fire_after(fired, last_fire=fired) == fired + _DAY


def test_daily_last_fire_in_past_keeps_next_occurrence() -> None:
    s = Schedule.parse("00:00", tz_offset_seconds=0)
    # last_fire given but strictly before the candidate ⇒ inner guard is False.
    assert s.next_fire_after(1.0, last_fire=-_DAY) == float(_DAY)


# ────────────────────────────────────────────────────────────────────────
# seconds_until
# ────────────────────────────────────────────────────────────────────────


def test_seconds_until_positive() -> None:
    s = Schedule.parse("1h")
    assert s.seconds_until(1_000.0) == 3_600.0


def test_seconds_until_clamped_to_zero_when_caught_up() -> None:
    s = Schedule.parse("1h")
    assert s.seconds_until(10_000.0, last_fire=0.0) == 0.0


# ────────────────────────────────────────────────────────────────────────
# CheckpointView.from_loaded
# ────────────────────────────────────────────────────────────────────────


def test_view_from_loaded_none() -> None:
    v = CheckpointView.from_loaded(None, expected_sweep_id="grid-a")
    assert v == CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)


def test_view_from_loaded_matching() -> None:
    ckpt = SweepCheckpoint(
        sweep_id="grid-a", total_combos=10, last_completed_index=3, updated_at=1.0
    )
    v = CheckpointView.from_loaded(ckpt, expected_sweep_id="grid-a")
    assert v.exists is True
    assert v.sweep_id_matches is True
    assert v.next_index == 4
    assert v.total_combos == 10


def test_view_from_loaded_mismatching() -> None:
    ckpt = SweepCheckpoint(
        sweep_id="grid-b", total_combos=10, last_completed_index=3, updated_at=1.0
    )
    v = CheckpointView.from_loaded(ckpt, expected_sweep_id="grid-a")
    assert v.exists is True
    assert v.sweep_id_matches is False


# ────────────────────────────────────────────────────────────────────────
# decide_resume + should_run
# ────────────────────────────────────────────────────────────────────────


def test_decide_resume_fresh_no_checkpoint() -> None:
    d = decide_resume(
        CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)
    )
    assert d.kind is ResumeKind.FRESH
    assert d.resume_from_index == 0
    assert d.should_run is True
    assert "no checkpoint" in d.reason


def test_decide_resume_fresh_wrong_identity() -> None:
    d = decide_resume(
        CheckpointView(exists=True, sweep_id_matches=False, next_index=3, total_combos=10)
    )
    assert d.kind is ResumeKind.FRESH
    assert d.resume_from_index == 0
    assert "different grid identity" in d.reason


def test_decide_resume_already_complete() -> None:
    d = decide_resume(
        CheckpointView(exists=True, sweep_id_matches=True, next_index=10, total_combos=10)
    )
    assert d.kind is ResumeKind.ALREADY_COMPLETE
    assert d.resume_from_index == 10
    assert d.should_run is False
    assert "already covers" in d.reason


def test_decide_resume_resume_in_progress() -> None:
    d = decide_resume(
        CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10)
    )
    assert d.kind is ResumeKind.RESUME
    assert d.resume_from_index == 3
    assert d.should_run is True
    assert "resuming from tuple 3" in d.reason


# ────────────────────────────────────────────────────────────────────────
# plan_cycle
# ────────────────────────────────────────────────────────────────────────


def test_plan_cycle_run_immediately() -> None:
    s = Schedule.parse("24h")
    view = CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)
    plan = plan_cycle(now=1_000.0, schedule=s, checkpoint_view=view, run_immediately=True)
    assert isinstance(plan, CyclePlan)
    assert plan.fire_at == 1_000.0
    assert plan.sleep_seconds == 0.0
    assert plan.resume.kind is ResumeKind.FRESH


def test_plan_cycle_scheduled_positive_sleep() -> None:
    s = Schedule.parse("1h")
    view = CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10)
    plan = plan_cycle(now=1_000.0, schedule=s, checkpoint_view=view)
    assert plan.fire_at == 1_000.0 + 3_600
    assert plan.sleep_seconds == 3_600.0
    assert plan.resume.kind is ResumeKind.RESUME


def test_plan_cycle_scheduled_zero_sleep_when_caught_up() -> None:
    s = Schedule.parse("1h")
    view = CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0)
    plan = plan_cycle(now=10_000.0, schedule=s, checkpoint_view=view, last_fire=0.0)
    assert plan.fire_at == 10_000.0  # caught up ⇒ fire now
    assert plan.sleep_seconds == 0.0


# ────────────────────────────────────────────────────────────────────────
# cycle_audit_payload
# ────────────────────────────────────────────────────────────────────────


def test_cycle_audit_payload_shape() -> None:
    s = Schedule.parse("daily@02:30", tz_offset_seconds=IST_OFFSET_SECONDS)
    resume = decide_resume(
        CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10)
    )
    payload = cycle_audit_payload(
        cycle_index=2, fire_at=1_700_000_000.0, schedule=s, resume=resume, once=False
    )
    assert payload == {
        "cycle_index": 2,
        "fire_at": 1_700_000_000.0,
        "schedule_kind": "daily",
        "schedule_spec": "daily@02:30",
        "interval_seconds": 0,
        "second_of_day": 2 * 3_600 + 30 * 60,
        "tz_offset_seconds": IST_OFFSET_SECONDS,
        "resume_kind": "resume",
        "resume_from_index": 3,
        "resume_reason": resume.reason,
        "should_run": True,
        "once": False,
    }


# ────────────────────────────────────────────────────────────────────────
# Hypothesis properties
# ────────────────────────────────────────────────────────────────────────


@given(
    interval=st.integers(min_value=1, max_value=7 * _DAY),
    now=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
    last=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_interval_next_fire_never_before_now(interval: int, now: float, last: float) -> None:
    s = Schedule.parse(f"{interval}s")
    assert s.next_fire_after(now, last_fire=last) >= now


@given(
    second_of_day=st.integers(min_value=0, max_value=_DAY - 1),
    tz=st.sampled_from([0, IST_OFFSET_SECONDS, -5 * 3_600]),
    now=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_daily_fire_lands_on_configured_wall_clock(second_of_day: int, tz: int, now: float) -> None:
    s = Schedule(
        kind=ScheduleKind.DAILY,
        interval_seconds=0,
        second_of_day=second_of_day,
        tz_offset_seconds=tz,
        spec="prop",
    )
    nf = s.next_fire_after(now)
    assert nf >= now
    # The fire always lands exactly on the configured local second-of-day.
    assert int(nf + tz) % _DAY == second_of_day


@given(
    second_of_day=st.integers(min_value=0, max_value=_DAY - 1),
    now=st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False),
)
def test_daily_recompute_after_fire_never_repeats(second_of_day: int, now: float) -> None:
    s = Schedule(
        kind=ScheduleKind.DAILY,
        interval_seconds=0,
        second_of_day=second_of_day,
        tz_offset_seconds=0,
        spec="prop",
    )
    fired = s.next_fire_after(now)
    nxt = s.next_fire_after(fired, last_fire=fired)
    assert nxt > fired
    assert math.isclose(nxt - fired, _DAY)


class TestSweepOpsRouting:
    """Flag-ON routing parity for the next-fire arithmetic + resume fold.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    behavior-identical to the flag-OFF Python laws (which every OTHER test
    in this file drives) — including the daily arm's ``int`` return TYPE
    (the daemon's ``json.dumps`` audit bytes) and every reason string BYTE;
    ``Schedule.parse`` (regex marshal-land) stays Python on both arms.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _drive() -> list[object]:
        import json

        interval = Schedule.parse("30m")
        daily = Schedule.parse("daily@02:30", tz_offset_seconds=IST_OFFSET_SECONDS)
        now = 1_728_000_000.0
        outcomes: list[object] = []
        for schedule in (interval, daily):
            fire = schedule.next_fire_after(now, last_fire=None)
            refire = schedule.next_fire_after(float(fire), last_fire=float(fire))
            outcomes.append((fire, type(fire).__name__, refire, type(refire).__name__))
            outcomes.append(schedule.seconds_until(now))
            plan = plan_cycle(
                now=now,
                schedule=schedule,
                checkpoint_view=CheckpointView(
                    exists=True, sweep_id_matches=True, next_index=3, total_combos=10
                ),
            )
            outcomes.append(
                json.dumps(
                    cycle_audit_payload(
                        cycle_index=0,
                        fire_at=plan.fire_at,
                        schedule=schedule,
                        resume=plan.resume,
                        once=False,
                    ),
                    sort_keys=True,
                )
            )
        views = [
            CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0),
            CheckpointView(exists=True, sweep_id_matches=False, next_index=3, total_combos=10),
            CheckpointView(exists=True, sweep_id_matches=True, next_index=10, total_combos=10),
            CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10),
        ]
        outcomes.extend(decide_resume(view) for view in views)
        return outcomes

    def test_flag_on_matches_flag_off_including_audit_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = self._drive()
        self._arm_fake_crate(monkeypatch)
        try:
            on = self._drive()
        finally:
            self._reset_facade()
        assert on == off
        # The daily arm's fire epoch is an INT on both arms (audit-bytes law).
        assert on[3][1] == "int" and on[3][3] == "int"  # type: ignore[index]
        assert on[0][1] == "float" and on[0][3] == "float"  # type: ignore[index]

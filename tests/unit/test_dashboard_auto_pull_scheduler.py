# pyright: reportPrivateUsage=none
"""Unit tests for :mod:`brutex.dashboard.auto_pull_scheduler`.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779, round-5 fold) — the
self-arming post-market daily auto-pull scheduler, at 100% line + branch on
its own ci.yml gate. Frozen clocks + injected gate/launcher/state-store
seams everywhere; the only threads are the two REAL run-loop tests, which
synchronize on bounded ``threading.Event`` waits (never sleeps-as-sync).

Pinned semantics (the round-5 laws):

* ONE STARTED attempt per day (``last_attempt_date``), whatever the run's
  outcome — a started-but-FAILED run retries on the NEXT eligible day;
* a REFUSED launch (single-flight / sweep contention) marks NOTHING and is
  retried on later ticks (the deferral catch-up);
* ``last_success_date`` moves ONLY through :meth:`note_run_complete` with
  ``ok=True``;
* a corrupt / unreadable state file reads DISARMED with ONE WARN;
* disarm persists + stops firing; zero threads until armed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pytest
from brutex.calendars.auto_pull_gate import PULL_ALL_SETTLE_ENV, AutoGateVerdict
from brutex.dashboard.auto_pull_scheduler import (
    AUTO_PULL_STATE_FILENAME,
    AUTO_PULL_STATE_SCHEMA,
    AUTO_PULL_TICK_ENV,
    DEFAULT_TICK_SECONDS,
    AutoLaunchResult,
    AutoPullScheduler,
    AutoPullState,
    AutoPullStateStore,
    resolve_tick_seconds,
)
from structlog.testing import capture_logs

# Monday 2026-07-13 is a real NSE trading day (holiday coverage runs through
# 2026); 18:00 is past the conservative 17:30 settle default.
_OPEN = datetime(2026, 7, 13, 18, 0)
_BEFORE_SETTLE = datetime(2026, 7, 13, 12, 0)
_SUNDAY = datetime(2026, 7, 12, 18, 0)
_NEXT_DAY_OPEN = datetime(2026, 7, 14, 18, 0)


def _allowed_gate(_instant: datetime) -> AutoGateVerdict:
    return AutoGateVerdict(allowed=True, reason="ok", detail="ok")


def _deferred_gate(reason: str, detail: str) -> Any:
    def gate(_instant: datetime) -> AutoGateVerdict:
        return AutoGateVerdict(allowed=False, reason=reason, detail=detail)

    return gate


class _Launcher:
    """A scripted launcher: yields the queued results, repeating the last."""

    def __init__(self, *results: AutoLaunchResult) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self) -> AutoLaunchResult:
        self.calls += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _started() -> AutoLaunchResult:
    return AutoLaunchResult(started=True, detail="spot-only pipeline job started")


def _refused() -> AutoLaunchResult:
    return AutoLaunchResult(started=False, detail="launch refused — the pull single-flight is held")


def _scheduler(
    tmp_path: Path,
    *,
    launcher: Any = None,
    gate: Any = None,
    clock: Any = None,
    tick_seconds: float = 3600.0,
    env: dict[str, str] | None = None,
) -> tuple[AutoPullScheduler, AutoPullStateStore]:
    store = AutoPullStateStore(tmp_path)
    scheduler = AutoPullScheduler(
        store,
        launcher=launcher,
        gate=gate,
        clock=clock,
        tick_seconds=tick_seconds,
        env={} if env is None else env,
    )
    return scheduler, store


# ── resolve_tick_seconds ─────────────────────────────────────────────────────


class TestResolveTickSeconds:
    def test_unset_and_blank_default(self) -> None:
        assert resolve_tick_seconds({}) == DEFAULT_TICK_SECONDS
        assert resolve_tick_seconds({AUTO_PULL_TICK_ENV: "   "}) == DEFAULT_TICK_SECONDS

    def test_valid_parses(self) -> None:
        assert resolve_tick_seconds({AUTO_PULL_TICK_ENV: "30"}) == 30.0
        assert resolve_tick_seconds({AUTO_PULL_TICK_ENV: " 0.5 "}) == 0.5

    @pytest.mark.parametrize("junk", ["abc", "1m", ""])
    def test_junk_raises(self, junk: str) -> None:
        if not junk:
            # blank is the DEFAULT arm, asserted above — keep the matrix honest.
            assert resolve_tick_seconds({AUTO_PULL_TICK_ENV: junk}) == DEFAULT_TICK_SECONDS
            return
        with pytest.raises(ValueError, match="is not a number"):
            resolve_tick_seconds({AUTO_PULL_TICK_ENV: junk})

    @pytest.mark.parametrize("bad", ["inf", "nan", "-inf", "0", "-5"])
    def test_nonfinite_or_nonpositive_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="positive finite"):
            resolve_tick_seconds({AUTO_PULL_TICK_ENV: bad})

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTO_PULL_TICK_ENV, "45")
        assert resolve_tick_seconds() == 45.0


# ── AutoPullStateStore ───────────────────────────────────────────────────────


class TestStateStore:
    def test_missing_file_is_disarmed_default_no_warn(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        with capture_logs() as logs:
            state = store.load()
        assert state == AutoPullState()
        assert state.armed is False
        assert logs == []

    def test_path_property(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        assert store.path == tmp_path / AUTO_PULL_STATE_FILENAME

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        state = AutoPullState(
            armed=True,
            armed_at="2026-07-13T18:00:00",
            last_attempt_date="2026-07-13",
            last_success_date="2026-07-12",
        )
        assert store.save(state) is True
        assert AutoPullStateStore(tmp_path).load() == state
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["schema"] == AUTO_PULL_STATE_SCHEMA

    def test_corrupt_json_disarmed_one_warn_only(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        store.path.write_text("{not json", encoding="utf-8")
        with capture_logs() as logs:
            assert store.load() == AutoPullState()
            assert store.load() == AutoPullState()  # once-flag: no second WARN
        corrupt = [entry for entry in logs if entry["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["log_level"] == "warning"

    def test_clean_load_resets_the_once_flag(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        store.path.write_text("junk", encoding="utf-8")
        with capture_logs() as logs:
            store.load()  # WARN 1
            assert store.save(AutoPullState(armed=True)) is True
            assert store.load().armed is True  # clean → resets the flag
            store.path.write_text("junk again", encoding="utf-8")
            store.load()  # WARN 2 — a NEW corruption sighting warns again
        corrupt = [entry for entry in logs if entry["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 2

    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            ('["not", "an", "object"]', "not a JSON object"),
            ('{"schema": 99, "armed": true}', "unknown schema"),
            (f'{{"schema": {AUTO_PULL_STATE_SCHEMA}, "armed": "yes"}}', "armed is not a bool"),
            (
                f'{{"schema": {AUTO_PULL_STATE_SCHEMA}, "armed": true, "armed_at": 7}}',
                "armed_at is not a string",
            ),
        ],
    )
    def test_shape_corruption_matrix(self, tmp_path: Path, body: str, detail: str) -> None:
        store = AutoPullStateStore(tmp_path)
        store.path.write_text(body, encoding="utf-8")
        with capture_logs() as logs:
            assert store.load() == AutoPullState()
        corrupt = [entry for entry in logs if entry["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert detail in str(corrupt[0]["detail"])

    def test_missing_optional_fields_default_none(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        store.path.write_text(
            f'{{"schema": {AUTO_PULL_STATE_SCHEMA}, "armed": true}}', encoding="utf-8"
        )
        state = store.load()
        assert state.armed is True
        assert state.armed_at is None
        assert state.last_attempt_date is None
        assert state.last_success_date is None

    def test_unreadable_file_disarmed_warn(self, tmp_path: Path) -> None:
        store = AutoPullStateStore(tmp_path)
        store.path.mkdir()  # a DIRECTORY at the path → OSError on read_text
        with capture_logs() as logs:
            assert store.load() == AutoPullState()
        corrupt = [entry for entry in logs if entry["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert "unreadable" in str(corrupt[0]["detail"])

    def test_save_failure_logs_and_returns_false(self, tmp_path: Path) -> None:
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("file where the lake root should be", encoding="utf-8")
        store = AutoPullStateStore(blocked)
        with capture_logs() as logs:
            assert store.save(AutoPullState(armed=True)) is False
        failed = [entry for entry in logs if entry["event"] == "auto_pull.state_write_failed"]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"


# ── the scheduler decision surface (tick_once) ───────────────────────────────


class TestTickOnce:
    def test_disarmed_tick_is_inert(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        assert scheduler.tick_once(now=_OPEN) == "disarmed"
        assert launcher.calls == 0

    def test_corrupt_state_reads_disarmed_with_warn(self, tmp_path: Path) -> None:
        (tmp_path / AUTO_PULL_STATE_FILENAME).write_text("garbage", encoding="utf-8")
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        with capture_logs() as logs:
            assert scheduler.tick_once(now=_OPEN) == "disarmed"
        assert launcher.calls == 0
        assert [entry["event"] for entry in logs] == ["auto_pull.state_corrupt"]

    def test_gate_open_fires_exactly_once_per_day(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        with capture_logs() as logs:
            assert scheduler.tick_once(now=_OPEN) == "fired"
            # The once-per-day guard: later same-day ticks never re-fire.
            assert scheduler.tick_once(now=_OPEN) == "already_attempted"
            assert scheduler.tick_once(now=_OPEN) == "already_attempted"
        scheduler.stop()
        assert launcher.calls == 1
        assert AutoPullStateStore(tmp_path).load().last_attempt_date == "2026-07-13"
        fired = [entry for entry in logs if entry["event"] == "auto_pull.fired"]
        assert len(fired) == 1

    def test_success_marks_day_done_and_next_day_fires_again(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        assert scheduler.tick_once(now=_OPEN) == "fired"
        scheduler.note_run_complete(ok=True, run_date=_OPEN.date(), source="auto")
        scheduler.stop()
        assert scheduler.tick_once(now=_OPEN) == "already_succeeded"
        assert AutoPullStateStore(tmp_path).load().last_success_date == "2026-07-13"
        # The next day is a fresh slate (auto-incremental daily convergence).
        assert scheduler.tick_once(now=_NEXT_DAY_OPEN) == "fired"
        assert launcher.calls == 2

    def test_failed_run_does_not_mark_success_and_retries_next_day(self, tmp_path: Path) -> None:
        """PINNED: a STARTED-but-FAILED run burns the day's attempt (no same-day
        retry storm) and retries on the NEXT eligible day — the incremental
        ledger heals the gap."""
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        assert scheduler.tick_once(now=_OPEN) == "fired"
        with capture_logs() as logs:
            scheduler.note_run_complete(ok=False, run_date=_OPEN.date(), source="auto")
        scheduler.stop()
        completed = [entry for entry in logs if entry["event"] == "auto_pull.completed"]
        assert len(completed) == 1
        assert completed[0]["ok"] is False
        state = AutoPullStateStore(tmp_path).load()
        assert state.last_success_date is None  # a FAILED run never marks success
        assert state.last_attempt_date == "2026-07-13"
        assert scheduler.tick_once(now=_OPEN) == "already_attempted"
        assert scheduler.tick_once(now=_NEXT_DAY_OPEN) == "fired"

    def test_refused_launch_retries_then_succeeds(self, tmp_path: Path) -> None:
        """Lock contention (e.g. a multi-day dashboard sweep holds the flock +
        job slot whole-run): refused ⇒ NOT an attempt ⇒ later ticks retry —
        and the retry that starts marks the day."""
        launcher = _Launcher(_refused(), _refused(), _started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        with capture_logs() as logs:
            assert scheduler.tick_once(now=_OPEN) == "refused_retry"
            assert AutoPullStateStore(tmp_path).load().last_attempt_date is None
            assert scheduler.tick_once(now=_OPEN) == "refused_retry"
            assert scheduler.tick_once(now=_OPEN) == "fired"
        scheduler.stop()
        assert launcher.calls == 3
        assert AutoPullStateStore(tmp_path).load().last_attempt_date == "2026-07-13"
        # The refusal WARNs once per (day, reason) — never once per tick.
        refused = [entry for entry in logs if entry["event"] == "auto_pull.refused_retry"]
        assert len(refused) == 1
        assert refused[0]["log_level"] == "warning"

    def test_deferred_logs_once_per_day_per_reason(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        gate_reason = {"reason": "before_settle", "detail": "waiting for settle"}

        def gate(_instant: datetime) -> AutoGateVerdict:
            return AutoGateVerdict(allowed=False, **gate_reason)  # type: ignore[arg-type]

        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=gate)
        scheduler.arm(now=_BEFORE_SETTLE)
        with capture_logs() as logs:
            assert scheduler.tick_once(now=_BEFORE_SETTLE) == "deferred:before_settle"
            assert scheduler.tick_once(now=_BEFORE_SETTLE) == "deferred:before_settle"
            gate_reason.update(reason="non_trading_day", detail="holiday")
            assert scheduler.tick_once(now=_BEFORE_SETTLE) == "deferred:non_trading_day"
            # A NEW day resets the memo — the same reason logs again.
            gate_reason.update(reason="before_settle", detail="waiting for settle")
            assert scheduler.tick_once(now=_NEXT_DAY_OPEN) == "deferred:before_settle"
        scheduler.stop()
        assert launcher.calls == 0
        deferred = [entry for entry in logs if entry["event"] == "auto_pull.deferred"]
        assert [entry["reason"] for entry in deferred] == [
            "before_settle",
            "non_trading_day",
            "before_settle",
        ]
        assert all(entry["log_level"] == "info" for entry in deferred)

    def test_unbound_launcher_refuses_and_retries(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        with capture_logs() as logs:
            assert scheduler.tick_once(now=_OPEN) == "refused_retry"
        scheduler.stop()
        refused = [entry for entry in logs if entry["event"] == "auto_pull.refused_retry"]
        assert len(refused) == 1
        assert refused[0]["reason"] == "launcher_unbound"
        # bind_launcher heals it — the next tick fires.
        launcher = _Launcher(_started())
        scheduler.bind_launcher(launcher)
        assert scheduler.tick_once(now=_OPEN) == "fired"

    def test_never_fires_intraday_real_gate(self, tmp_path: Path) -> None:
        """The REAL gate over the 09:15–15:30 trading grid: an armed scheduler
        never fires inside the session — every tick defers before settle."""
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=None)
        scheduler.arm(now=datetime(2026, 7, 13, 9, 0))
        for hh, mm in [(9, 15), (10, 0), (12, 0), (14, 30), (15, 0), (15, 30)]:
            token = scheduler.tick_once(now=datetime(2026, 7, 13, hh, mm))
            assert token == "deferred:before_settle", (hh, mm)
        scheduler.stop()
        assert launcher.calls == 0

    def test_real_gate_weekend_defers_and_post_settle_fires(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=None)
        scheduler.arm(now=_SUNDAY)
        assert scheduler.tick_once(now=_SUNDAY) == "deferred:non_trading_day"
        assert scheduler.tick_once(now=_OPEN) == "fired"
        scheduler.stop()

    def test_default_clock_used_when_now_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("brutex.dashboard.auto_pull_scheduler.now_ist", lambda: _OPEN)
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate, clock=None)
        scheduler.arm()
        assert scheduler.tick_once() == "fired"
        scheduler.stop()

    def test_state_loaded_once_then_mirrored(self, tmp_path: Path) -> None:
        """SINGLE-WRITER mirror: the sidecar is read once lazily; repeat
        reads are O(1) memory (no per-poll lake I/O)."""
        scheduler, store = _scheduler(tmp_path, gate=_allowed_gate)
        loads = 0
        real_load = store.load

        def counting_load() -> AutoPullState:
            nonlocal loads
            loads += 1
            return real_load()

        store.load = counting_load  # type: ignore[method-assign]
        assert scheduler.tick_once(now=_OPEN) == "disarmed"
        assert scheduler.tick_once(now=_OPEN) == "disarmed"
        scheduler.status(now=_OPEN)
        assert loads == 1


# ── arming / disarming ───────────────────────────────────────────────────────


class TestArmDisarm:
    def test_arm_persists_logs_and_starts_ticker(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate, clock=lambda: _OPEN)
        with capture_logs() as logs:
            state = scheduler.arm()
        try:
            assert state.armed is True
            assert state.armed_at == _OPEN.isoformat()
            on_disk = AutoPullStateStore(tmp_path).load()
            assert on_disk.armed is True  # durable across restarts
            assert [entry["event"] for entry in logs] == ["auto_pull.armed"]
            threads = [t.name for t in threading.enumerate()]
            assert "brutex-auto-pull-scheduler" in threads
        finally:
            scheduler.stop()

    def test_disarm_persists_and_stops_firing(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        with capture_logs() as logs:
            state = scheduler.disarm()
        scheduler.stop()
        assert state.armed is False
        assert state.armed_at is None
        assert AutoPullStateStore(tmp_path).load().armed is False  # durable
        assert [entry["event"] for entry in logs] == ["auto_pull.disarmed"]
        assert scheduler.tick_once(now=_OPEN) == "disarmed"
        assert launcher.calls == 0


# ── thread lifecycle ─────────────────────────────────────────────────────────


class TestThreadLifecycle:
    def test_ensure_started_idempotent(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate)
        try:
            assert scheduler.ensure_started() is True
            assert scheduler.ensure_started() is False  # double-wire safe
        finally:
            scheduler.stop()

    def test_stop_before_start_is_safe_and_restart_spawns_fresh(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate)
        scheduler.stop()  # never started — no raise
        assert scheduler.ensure_started() is True
        scheduler.stop()
        assert scheduler.ensure_started() is True  # a stopped loop restarts fresh
        scheduler.stop()

    def test_start_if_armed_disarmed_spawns_nothing(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate)
        assert scheduler.start_if_armed() is False
        assert "brutex-auto-pull-scheduler" not in [t.name for t in threading.enumerate()]

    def test_start_if_armed_resumes_boot_catch_up(self, tmp_path: Path) -> None:
        """BOOT CATCH-UP: a durably-armed state file + an open window + today
        unsuccessful ⇒ the resumed loop fires on an early tick."""
        AutoPullStateStore(tmp_path).save(AutoPullState(armed=True, armed_at="x"))
        fired = threading.Event()

        def launcher() -> AutoLaunchResult:
            fired.set()
            return _started()

        scheduler, _ = _scheduler(
            tmp_path,
            launcher=launcher,
            gate=_allowed_gate,
            clock=lambda: _OPEN,
            tick_seconds=0.01,
        )
        try:
            assert scheduler.start_if_armed() is True
            assert fired.wait(timeout=10.0), "the resumed loop never fired"
        finally:
            scheduler.stop()
        assert AutoPullStateStore(tmp_path).load().last_attempt_date == "2026-07-13"

    def test_run_loop_survives_a_raising_tick(self, tmp_path: Path) -> None:
        """The never-crash loop guard: a raising gate logs auto_pull.tick_error
        and the loop keeps ticking (the dashboard is never crashed)."""
        calls = 0
        second_tick = threading.Event()

        def raising_gate(_instant: datetime) -> AutoGateVerdict:
            nonlocal calls
            calls += 1
            if calls >= 2:
                second_tick.set()
            raise RuntimeError("boom")

        scheduler, _ = _scheduler(tmp_path, gate=raising_gate, tick_seconds=0.01)
        scheduler.arm(now=_OPEN)
        try:
            assert second_tick.wait(timeout=10.0), "the loop died on the first raising tick"
        finally:
            scheduler.stop()


# ── status / next-eligible honesty ───────────────────────────────────────────


class TestStatus:
    def test_status_shape_and_disarmed_text(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate)
        status = scheduler.status(now=_OPEN)
        assert set(status) == {
            "armed",
            "armed_at",
            "last_attempt_date",
            "last_success_date",
            "last_outcome",
            "next_eligible",
        }
        assert status["armed"] is False
        assert status["next_eligible"].startswith("disarmed — ")

    def test_status_default_clock(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate, clock=lambda: _OPEN)
        assert scheduler.status()["armed"] is False

    def test_next_eligible_variants(self, tmp_path: Path) -> None:
        launcher = _Launcher(_started())
        scheduler, _ = _scheduler(tmp_path, launcher=launcher, gate=_allowed_gate)
        scheduler.arm(now=_OPEN)
        try:
            # armed + gate open + unattempted ⇒ eligible now.
            assert scheduler.status(now=_OPEN)["next_eligible"].startswith("eligible now")
            # started attempt ⇒ attempted-today wording (honest: no fabricated time).
            assert scheduler.tick_once(now=_OPEN) == "fired"
            assert scheduler.status(now=_OPEN)["next_eligible"].startswith("attempted today")
            assert scheduler.status(now=_OPEN)["last_outcome"] == "fired (2026-07-13)"
            # completed ok ⇒ done-for-today wording.
            scheduler.note_run_complete(ok=True, run_date=_OPEN.date(), source="auto")
            assert scheduler.status(now=_OPEN)["next_eligible"].startswith("done for today")
        finally:
            scheduler.stop()

    def test_next_eligible_deferred_passes_gate_detail_through(self, tmp_path: Path) -> None:
        gate = _deferred_gate("before_settle", "waiting for post-close settle time 17:30 IST")
        scheduler, _ = _scheduler(tmp_path, gate=gate)
        scheduler.arm(now=_BEFORE_SETTLE)
        try:
            assert (
                scheduler.status(now=_BEFORE_SETTLE)["next_eligible"]
                == "waiting for post-close settle time 17:30 IST"
            )
        finally:
            scheduler.stop()

    def test_next_eligible_junk_settle_env_is_honest(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(tmp_path, gate=_allowed_gate, env={PULL_ALL_SETTLE_ENV: "25:99"})
        scheduler.arm(now=_OPEN)
        try:
            text = scheduler.status(now=_OPEN)["next_eligible"]
            assert text.startswith("unknown — fix BRUTEX_PULL_ALL_SETTLE_IST")
        finally:
            scheduler.stop()

    def test_next_eligible_gate_error_is_honest_never_raises(self, tmp_path: Path) -> None:
        def raising_gate(_instant: datetime) -> AutoGateVerdict:
            raise RuntimeError("gate exploded")

        scheduler, _ = _scheduler(tmp_path, gate=raising_gate)
        scheduler.arm(now=_OPEN)
        try:
            text = scheduler.status(now=_OPEN)["next_eligible"]
            assert text == "unknown — auto gate error (RuntimeError: gate exploded)"
        finally:
            scheduler.stop()


# ── construction knobs ───────────────────────────────────────────────────────


class TestConstruction:
    def test_tick_seconds_resolved_from_env_when_omitted(self, tmp_path: Path) -> None:
        scheduler = AutoPullScheduler(AutoPullStateStore(tmp_path), env={AUTO_PULL_TICK_ENV: "120"})
        assert scheduler._tick_seconds == 120.0

    def test_junk_tick_env_fails_loud_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=AUTO_PULL_TICK_ENV):
            AutoPullScheduler(AutoPullStateStore(tmp_path), env={AUTO_PULL_TICK_ENV: "junk"})

    def test_default_gate_is_the_real_auto_pull_gate(self, tmp_path: Path) -> None:
        """gate=None wires the EXISTING auto_pull_allowed — reused, never
        re-derived (18:00 Monday allowed; noon defers before_settle)."""
        scheduler, _ = _scheduler(tmp_path, launcher=_Launcher(_started()), gate=None)
        scheduler.arm(now=_OPEN)
        try:
            assert scheduler.tick_once(now=_BEFORE_SETTLE) == "deferred:before_settle"
            assert scheduler.tick_once(now=_OPEN) == "fired"
        finally:
            scheduler.stop()

    def test_settle_time_env_threaded_to_the_default_gate(self, tmp_path: Path) -> None:
        scheduler, _ = _scheduler(
            tmp_path,
            launcher=_Launcher(_started()),
            gate=None,
            env={PULL_ALL_SETTLE_ENV: "19:00"},
        )
        scheduler.arm(now=_OPEN)
        try:
            assert scheduler.tick_once(now=_OPEN) == "deferred:before_settle"
            assert scheduler.tick_once(now=datetime(2026, 7, 13, 19, 0)) == "fired"
        finally:
            scheduler.stop()

    def test_settle_env_shape(self) -> None:
        # Belt: the env constant the status text names is the gate module's.
        assert PULL_ALL_SETTLE_ENV == "BRUTEX_PULL_ALL_SETTLE_IST"
        assert isinstance(time(17, 30), time)

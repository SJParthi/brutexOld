"""Unit tests for :mod:`brutex.backup.deadman` (DEC-BACKUP-OFFBOX-001 §5.4/§7).

Pure core: every verdict of :func:`evaluate_deadman` (the evaluation-order
table) with an injected clock — both sides of the 300 s skew boundary, the
RUNNING/HUNG max-runtime boundary, the exact-threshold FRESH boundary, and the
durable cross-run re-alert bound. Shell: alert kind/severity/dedupe, the
only-stamp-on-success rule (F22), corrupt-state forensics preservation (F13 +
§15 R3), the REAL ``_default_ping`` against a live ``http.server`` thread
(F18), the F21 deep cross-check tiers, and the dry-run zero-side-effect pin.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog.testing
from brutex.backup.deadman import (
    ALARM_VERDICTS,
    DEEP_CHECK_TIMEOUT_S,
    DeadmanVerdict,
    _deep_divergence,
    _default_ping,
    _default_utc_clock,
    _snapshot_ids_match,
    evaluate_deadman,
    run_deadman_check,
)
from brutex.backup.errors import EXIT_DEADMAN_BREACH, EXIT_OK
from brutex.backup.restic_runner import ResticInvocation, ResticRunner
from brutex.backup.state import (
    BackupState,
    DeadmanState,
    deadman_path,
    empty_backup_state,
    load_deadman,
    save_deadman,
    save_state,
    state_path,
)
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEventKind, TelegramSeverity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from brutex.notifications.telegram import TelegramEvent

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


def _state(**overrides: Any) -> BackupState:
    """A baseline successful state (success 1 h ago), field-overridable."""
    base = replace(
        empty_backup_state(),
        last_attempt_at=NOW - timedelta(hours=1),
        last_success_at=NOW - timedelta(hours=1),
        last_snapshot_id="ab12",
    )
    return replace(base, **overrides)


def _evaluate(
    state: BackupState | None,
    sidecar: DeadmanState | None = None,
    *,
    threshold_hours: float = 30.0,
    realert_hours: float = 6.0,
    skew_tolerance_s: float = 300.0,
    max_runtime_hours: float = 48.0,
    state_error: str | None = None,
) -> Any:
    return evaluate_deadman(
        state,
        sidecar if sidecar is not None else DeadmanState(),
        threshold_hours=threshold_hours,
        realert_hours=realert_hours,
        skew_tolerance_s=skew_tolerance_s,
        max_runtime_hours=max_runtime_hours,
        clock=_clock,
        state_error=state_error,
    )


class TestEvaluateVerdicts:
    def test_fresh(self) -> None:
        report = _evaluate(_state())
        assert report.verdict is DeadmanVerdict.FRESH
        assert report.hours_since_success == pytest.approx(1.0)
        assert report.threshold_hours == 30.0
        assert report.should_alert is False

    def test_overdue(self) -> None:
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=31)))
        assert report.verdict is DeadmanVerdict.OVERDUE
        assert report.should_alert is True
        assert "31.0 h ago" in report.detail

    def test_boundary_exactly_threshold_is_fresh(self) -> None:
        """elapsed == threshold is FRESH (strict > for OVERDUE)."""
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=30)))
        assert report.verdict is DeadmanVerdict.FRESH

    def test_never_succeeded_state_none(self) -> None:
        report = _evaluate(None)
        assert report.verdict is DeadmanVerdict.NEVER_SUCCEEDED
        assert report.hours_since_success is None
        assert report.should_alert is True

    def test_never_succeeded_success_none(self) -> None:
        report = _evaluate(_state(last_success_at=None))
        assert report.verdict is DeadmanVerdict.NEVER_SUCCEEDED
        assert "no success ever stamped" in report.detail

    def test_state_corrupt_wins_over_all(self) -> None:
        """state_error beats a present state with running + future stamps (rule 1)."""
        poisoned = _state(
            running_since=NOW + timedelta(hours=9),
            last_success_at=NOW + timedelta(hours=9),
        )
        report = _evaluate(poisoned, state_error="bad json")
        assert report.verdict is DeadmanVerdict.STATE_CORRUPT
        assert report.should_alert is True
        assert "bad json" in report.detail

    def test_clock_skew_future_success_stamp_alarms(self) -> None:
        """A future stamp beyond 300 s is the poisoning vector — hard alarm (F14)."""
        report = _evaluate(_state(last_success_at=NOW + timedelta(seconds=301)))
        assert report.verdict is DeadmanVerdict.CLOCK_SKEW
        assert report.should_alert is True
        assert report.hours_since_success is not None
        assert report.hours_since_success < 0  # honest negative age

    def test_clock_skew_within_tolerance_is_fresh(self) -> None:
        """A future stamp WITHIN 300 s is benign NTP jitter → FRESH (elapsed clamps 0)."""
        report = _evaluate(_state(last_success_at=NOW + timedelta(seconds=299)))
        assert report.verdict is DeadmanVerdict.FRESH

    def test_running_fresh_no_alert(self) -> None:
        """A marked-running backup below max runtime NEVER pages (F17b — the seed)."""
        report = _evaluate(_state(running_since=NOW - timedelta(hours=20)))
        assert report.verdict is DeadmanVerdict.RUNNING
        assert report.should_alert is False

    def test_hung_at_max_runtime(self) -> None:
        """elapsed == max_runtime is HUNG (RUNNING needs strictly <)."""
        report = _evaluate(_state(running_since=NOW - timedelta(hours=48)))
        assert report.verdict is DeadmanVerdict.HUNG
        assert report.should_alert is True
        assert "wedged" in report.detail

    def test_running_since_future_is_clock_skew(self) -> None:
        report = _evaluate(_state(running_since=NOW + timedelta(hours=1)))
        assert report.verdict is DeadmanVerdict.CLOCK_SKEW
        assert "running_since" in report.detail

    def test_running_since_future_within_tolerance_is_running(self) -> None:
        """A running_since a few seconds ahead (NTP jitter) still reads RUNNING."""
        report = _evaluate(_state(running_since=NOW + timedelta(seconds=100)))
        assert report.verdict is DeadmanVerdict.RUNNING

    def test_alarm_verdict_set(self) -> None:
        """RUNNING/FRESH are the only non-alarm verdicts (the no-page pin)."""
        assert (
            frozenset(
                {
                    DeadmanVerdict.HUNG,
                    DeadmanVerdict.OVERDUE,
                    DeadmanVerdict.NEVER_SUCCEEDED,
                    DeadmanVerdict.STATE_CORRUPT,
                    DeadmanVerdict.CLOCK_SKEW,
                }
            )
            == ALARM_VERDICTS
        )


class TestRealertBound:
    def test_realert_first_breach_alerts(self) -> None:
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=40)))
        assert report.should_alert is True

    def test_realert_within_window_suppressed(self) -> None:
        sidecar = DeadmanState(last_overdue_alert_at=NOW - timedelta(hours=5))
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=40)), sidecar)
        assert report.verdict is DeadmanVerdict.OVERDUE  # verdict unchanged
        assert report.should_alert is False  # page rate bounded

    def test_realert_at_window_boundary_alerts(self) -> None:
        """now − last_alert == realert_hours re-alerts (≥, never zero — §7.3)."""
        sidecar = DeadmanState(last_overdue_alert_at=NOW - timedelta(hours=6))
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=40)), sidecar)
        assert report.should_alert is True

    def test_realert_future_stamp_alerts_fail_toward_alarm(self) -> None:
        """Round-2 finding 7: a FUTURE last_overdue_alert_at (botched restore /
        backward NTP step — the F14 vectors, one file over) reads as "no prior
        alert" — the Telegram lane must never be muted until the wall clock
        catches up with a poisoned stamp."""
        sidecar = DeadmanState(last_overdue_alert_at=NOW + timedelta(days=365))
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=100)), sidecar)
        assert report.verdict is DeadmanVerdict.OVERDUE
        assert report.should_alert is True

    def test_realert_marginally_future_stamp_alerts(self) -> None:
        """Even a slightly-future stamp alerts — the safe direction is one
        extra page (a successful publish restamps with the current clock)."""
        sidecar = DeadmanState(last_overdue_alert_at=NOW + timedelta(seconds=1))
        report = _evaluate(_state(last_success_at=NOW - timedelta(hours=40)), sidecar)
        assert report.should_alert is True

    def test_fresh_never_should_alert(self) -> None:
        sidecar = DeadmanState(last_overdue_alert_at=NOW - timedelta(days=30))
        assert _evaluate(_state(), sidecar).should_alert is False

    def test_running_never_should_alert(self) -> None:
        sidecar = DeadmanState(last_overdue_alert_at=NOW - timedelta(days=30))
        report = _evaluate(_state(running_since=NOW - timedelta(hours=1)), sidecar)
        assert report.should_alert is False


class FakeNotifier:
    """Records every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


class FakeDeepRunner:
    """Duck-typed deep-tier runner — records snapshots calls, returns a scripted inv."""

    def __init__(self, stdout: str = "[]", *, ok: bool = True) -> None:
        self.stdout = stdout
        self.ok = ok
        self.tags: list[str] = []

    def snapshots_latest_json(self, *, tag: str = "brutex") -> ResticInvocation:
        """Return the scripted snapshots invocation."""
        self.tags.append(tag)
        return ResticInvocation(
            op="snapshots",
            argv=("restic", "snapshots"),
            ok=self.ok,
            returncode=0 if self.ok else 1,
            stdout_tail=self.stdout[-2000:],
            stderr_tail="",
            stdout_full=self.stdout,
            timed_out=False,
            binary_missing=False,
        )


def _factory(runner: FakeDeepRunner) -> Callable[[BackupEnvConfig], ResticRunner]:
    """Cast the duck-typed fake into the factory seam's declared type."""
    return cast("Callable[[BackupEnvConfig], ResticRunner]", lambda _cfg: runner)


def _refusing_factory() -> Callable[[BackupEnvConfig], ResticRunner]:
    """A factory that MUST NOT be called (pins the deep-tier gates)."""

    def _refuse(_cfg: BackupEnvConfig) -> ResticRunner:
        raise AssertionError("runner_factory must not be called on this tick")

    return _refuse


def _shell_cfg(tmp_path: Path, **env: str) -> BackupEnvConfig:
    env.setdefault("BRUTEX_BACKUP_STATE_DIR", str(tmp_path / "bstate"))
    return resolve_backup_env(env, home=tmp_path)


def _repo_env(tmp_path: Path) -> dict[str, str]:
    return {
        "BRUTEX_BACKUP_REPO": "s3:host/bucket",
        "BRUTEX_BACKUP_PASSWORD_FILE": str(tmp_path / "pw"),
    }


def _agreeing_snapshots_json() -> str:
    """Remote latest within threshold of the baseline state's success stamp."""
    stamp = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    return f'[{{"short_id": "ab12", "time": "{stamp}"}}]'


def _diverging_snapshots_json() -> str:
    stamp = (NOW - timedelta(hours=200)).isoformat().replace("+00:00", "Z")
    return f'[{{"short_id": "old1", "time": "{stamp}"}}]'


class TestCheckerShell:
    def test_check_survives_tilde_typo_env(self, tmp_path: Path) -> None:
        """Round-1 BLOCKER repro: a ``~<nonexistent-user>`` typo in the backup env
        must yield a VERDICT from the hourly tick, never a resolver traceback."""
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_PASSWORD_FILE="~no-such-user-x9/pw")
        assert run_deadman_check(cfg, dry_run=True) == EXIT_DEADMAN_BREACH  # NEVER_SUCCEEDED

    def test_alert_kind_severity_dedupe_key_recorded(self, tmp_path: Path) -> None:
        """OVERDUE → PHASE_BLOCKED / CRITICAL / backup-overdue:<reason> (§7.3/7.4)."""
        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        fake = FakeNotifier()
        code = run_deadman_check(
            cfg, alerter=SweepAlerter(notifier=fake), clock=_clock, ping=lambda url: True
        )
        assert code == EXIT_DEADMAN_BREACH
        assert len(fake.published) == 1
        event = fake.published[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.dedupe_key == "backup-overdue:overdue"
        assert "40\\.0 h ago" in event.message  # MarkdownV2-escaped age

    def test_publish_true_stamps_sidecar(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        run_deadman_check(
            cfg, alerter=SweepAlerter(notifier=FakeNotifier()), clock=_clock, ping=lambda u: True
        )
        assert load_deadman(deadman_path(cfg)).last_overdue_alert_at == NOW

    def test_publish_false_does_not_stamp(self, tmp_path: Path) -> None:
        """F22: a failed/disabled publish never stamps — the next tick retries."""
        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(
                cfg,
                alerter=SweepAlerter(resolver=lambda: None),
                clock=_clock,
                ping=lambda u: True,
            )
        assert code == EXIT_DEADMAN_BREACH
        assert load_deadman(deadman_path(cfg)).last_overdue_alert_at is None
        assert any(entry["event"] == "backup.alert.not_delivered" for entry in logs)

    def test_corrupt_state_never_overwritten_sidecar_still_throttles(self, tmp_path: Path) -> None:
        """F13 + §15 R3: forensics preserved AND the page rate bounded."""
        cfg = _shell_cfg(tmp_path)
        state_file = state_path(cfg)
        state_file.parent.mkdir(parents=True)
        poison = '{"schema": "poisoned-not-an-int"}'
        state_file.write_text(poison, encoding="utf-8")
        fake = FakeNotifier()
        code = run_deadman_check(
            cfg, alerter=SweepAlerter(notifier=fake), clock=_clock, ping=lambda u: True
        )
        assert code == EXIT_DEADMAN_BREACH
        assert state_file.read_text(encoding="utf-8") == poison  # NEVER overwritten
        assert fake.published[0].dedupe_key == "backup-overdue:state_corrupt"
        assert load_deadman(deadman_path(cfg)).last_overdue_alert_at == NOW
        # Second tick inside the realert window: throttled, no second page.
        fake2 = FakeNotifier()
        code2 = run_deadman_check(
            cfg, alerter=SweepAlerter(notifier=fake2), clock=_clock, ping=lambda u: True
        )
        assert code2 == EXIT_DEADMAN_BREACH
        assert fake2.published == []

    def test_bit_rot_state_alarms_state_corrupt_never_crashes(self, tmp_path: Path) -> None:
        """Round-4 MAJOR pin (F13's own named bit-rot vector): a single
        non-UTF-8 byte in state.json (one flipped high bit — the classic
        bit-rot/torn-sector shape on the pure-ASCII file) must yield the
        STATE_CORRUPT triple — exit 1 + CRITICAL alert + /fail ping — not a
        raw UnicodeDecodeError escaping the hourly checker forever."""
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL="http://hc.local/p")
        state_file = state_path(cfg)
        state_file.parent.mkdir(parents=True)
        poison = b'{"schema": 1\xff}'
        state_file.write_bytes(poison)
        fake = FakeNotifier()
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        code = run_deadman_check(cfg, alerter=SweepAlerter(notifier=fake), clock=_clock, ping=_ping)
        assert code == EXIT_DEADMAN_BREACH
        assert state_file.read_bytes() == poison  # forensics preserved
        assert fake.published[0].dedupe_key == "backup-overdue:state_corrupt"
        assert pings == ["http://hc.local/p/fail"]

    def test_bit_rot_sidecar_loads_empty_plus_warn(self, tmp_path: Path) -> None:
        """Round-4 MAJOR pin (sidecar arm): a non-UTF-8 deadman.json rides the
        corrupt→EMPTY (+ ONE WARN) contract instead of raising raw."""
        cfg = _shell_cfg(tmp_path)
        sidecar_file = deadman_path(cfg)
        sidecar_file.parent.mkdir(parents=True)
        sidecar_file.write_bytes(b'{"schema": 1\xff}')
        with structlog.testing.capture_logs() as logs:
            sidecar = load_deadman(sidecar_file)
        assert sidecar == DeadmanState()
        corrupt = [e for e in logs if e["event"] == "backup.deadman.sidecar_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == "UnicodeDecodeError"

    def test_sidecar_write_failure_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sidecar-write OSError can only cost an EXTRA alert — never a crash."""
        from brutex.backup import deadman as deadman_mod

        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state())

        def _raise(path: Path, state: DeadmanState) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(deadman_mod, "save_deadman", _raise)
        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock)
        assert code == EXIT_OK
        assert any(entry["event"] == "backup.deadman.sidecar_write_failed" for entry in logs)

    def test_no_ping_when_url_unset(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state())
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        code = run_deadman_check(
            cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock, ping=_ping
        )
        assert code == EXIT_OK
        assert pings == []

    def test_ping_fail_suffix_on_breach(self, tmp_path: Path) -> None:
        """Alarm verdicts ping <url>/fail; quiet verdicts ping the plain url (F18)."""
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL="http://hc.local/p")
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        run_deadman_check(
            cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock, ping=_ping
        )
        save_state(state_path(cfg), _state())
        run_deadman_check(
            cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock, ping=_ping
        )
        assert pings == ["http://hc.local/p/fail", "http://hc.local/p"]

    def test_dry_run_no_alert_no_sidecar_no_ping(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL="http://hc.local/p")
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        fake = FakeNotifier()
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        code = run_deadman_check(
            cfg,
            dry_run=True,
            alerter=SweepAlerter(notifier=fake),
            clock=_clock,
            ping=_ping,
            runner_factory=_refusing_factory(),
        )
        assert code == EXIT_DEADMAN_BREACH  # the verdict still reads true
        assert fake.published == []
        assert not deadman_path(cfg).exists()
        assert pings == []

    def test_dry_run_fresh_exit_ok(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path)
        save_state(state_path(cfg), _state())
        assert run_deadman_check(cfg, dry_run=True, clock=_clock) == EXIT_OK


class TestDeepTier:
    def test_deep_divergence_alarms(self, tmp_path: Path) -> None:
        """F21: remote latest far from the stamp → OVERDUE(state_remote_divergence)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        fake = FakeNotifier()
        runner = FakeDeepRunner(_diverging_snapshots_json())
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(notifier=fake),
            clock=_clock,
            runner_factory=_factory(runner),
        )
        assert code == EXIT_DEADMAN_BREACH
        assert runner.tags == ["brutex"]
        assert fake.published[0].dedupe_key == "backup-overdue:state_remote_divergence"
        # A COMPLETED comparison stamps the durable deep gate either way.
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW

    def test_deep_agreement_stamps_last_deep_check(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(
                cfg,
                deep=True,
                alerter=SweepAlerter(resolver=lambda: None),
                clock=_clock,
                runner_factory=_factory(FakeDeepRunner(_agreeing_snapshots_json())),
            )
        assert code == EXIT_OK
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW
        assert any(entry["event"] == "backup.deadman.deep_ok" for entry in logs)

    def test_deep_runner_gets_bounded_timeout_not_backup_deadline(self, tmp_path: Path) -> None:
        """Round-1 review pin: the deep tier's ONE metadata call runs under
        DEEP_CHECK_TIMEOUT_S (120 s), never the multi-day backup deadline —
        a wedged `restic snapshots` must not silence the hourly checker for 48 h."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        assert cfg.timeout_s == 48.0 * 3600.0  # the inherited default IS the hazard
        save_state(state_path(cfg), _state())
        runner = FakeDeepRunner(_agreeing_snapshots_json())
        seen: list[float] = []

        def _recording(c: BackupEnvConfig) -> ResticRunner:
            seen.append(c.timeout_s)
            return cast("ResticRunner", runner)

        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_recording,
        )
        assert code == EXIT_OK
        assert seen == [DEEP_CHECK_TIMEOUT_S]

    def test_deep_timeout_never_raised_above_operator_value(self, tmp_path: Path) -> None:
        """min() law: an operator timeout SMALLER than 120 s wins."""
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_TIMEOUT_S="60", **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        seen: list[float] = []

        def _recording(c: BackupEnvConfig) -> ResticRunner:
            seen.append(c.timeout_s)
            return cast("ResticRunner", FakeDeepRunner(_agreeing_snapshots_json()))

        run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_recording,
        )
        assert seen == [60.0]

    def test_deep_skipped_without_repo_config(self, tmp_path: Path) -> None:
        """Graft A pin: the shallow check needs NO repo config — deep logs + stands."""
        cfg = _shell_cfg(tmp_path)  # no repo env at all
        save_state(state_path(cfg), _state())
        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(
                cfg, deep=True, clock=_clock, runner_factory=_refusing_factory()
            )
        assert code == EXIT_OK
        skips = [e for e in logs if e["event"] == "backup.deadman.deep_skipped"]
        assert len(skips) == 1
        assert skips[0]["reason"] == "no_repo_config"

    def test_deep_auto_fires_after_24h_not_before(self, tmp_path: Path) -> None:
        """The durable last_deep_check_at gate — both branches."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        save_deadman(deadman_path(cfg), DeadmanState(last_deep_check_at=NOW - timedelta(hours=23)))
        code = run_deadman_check(cfg, clock=_clock, runner_factory=_refusing_factory())
        assert code == EXIT_OK  # 23 h — gate closed, factory untouched
        save_deadman(deadman_path(cfg), DeadmanState(last_deep_check_at=NOW - timedelta(hours=25)))
        runner = FakeDeepRunner(_agreeing_snapshots_json())
        code2 = run_deadman_check(cfg, clock=_clock, runner_factory=_factory(runner))
        assert code2 == EXIT_OK
        assert runner.tags == ["brutex"]  # 25 h — gate open, deep ran

    def test_deep_not_run_when_already_alarming(self, tmp_path: Path) -> None:
        """An already-alarming shallow verdict needs no divergence proof."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_refusing_factory(),
        )
        assert code == EXIT_DEADMAN_BREACH

    def test_deep_snapshots_failure_diverges_without_stamp(self, tmp_path: Path) -> None:
        """A failed metadata call alarms this tick but is RETRIED next tick (no stamp)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_factory(FakeDeepRunner(ok=False)),
        )
        assert code == EXIT_DEADMAN_BREACH
        assert load_deadman(deadman_path(cfg)).last_deep_check_at is None

    def test_deep_unparseable_output_diverges_with_stamp(self, tmp_path: Path) -> None:
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_factory(FakeDeepRunner("this is not json")),
        )
        assert code == EXIT_DEADMAN_BREACH
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW

    def test_deep_empty_repo_diverges(self, tmp_path: Path) -> None:
        """An empty snapshot list is a REAL answer — divergence, not assume-fine."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_factory(FakeDeepRunner("[]")),
        )
        assert code == EXIT_DEADMAN_BREACH

    def test_deep_divergence_defensive_no_success_branch(self, tmp_path: Path) -> None:
        """The defensive no-stamp branch of _deep_divergence (unreachable via FRESH)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        runner = cast("ResticRunner", FakeDeepRunner(_agreeing_snapshots_json()))
        diverged, completed, detail = _deep_divergence(cfg, _state(last_success_at=None), runner)
        assert diverged is True
        assert completed is True
        assert "no success stamp" in detail

    def test_deep_long_seed_matching_id_agrees(self, tmp_path: Path) -> None:
        """Round-3 MAJOR pin: the first armed tick after a multi-day 8 TB seed
        must NOT false-CRITICAL. restic stamps snapshot `time` at backup START
        (seed start, 194 h ago) while last_success_at lands at run END (2 h
        ago) — the gap IS the run duration, and the remote latest id equals
        state.last_snapshot_id, so the deep tier must read AGREEMENT: exit 0,
        plain healthcheck ping, deep gate stamped. The operator raised
        MAX_RUNTIME_HOURS for the seed window (runbook step 6) — the round-4
        stamp-plausibility bound uses the SAME knob, so the 192 h
        success-after-start overshoot stays within the raised bound."""
        cfg = _shell_cfg(
            tmp_path,
            BRUTEX_BACKUP_HEALTHCHECK_URL="http://hc.local/p",
            BRUTEX_BACKUP_MAX_RUNTIME_HOURS="200",
            **_repo_env(tmp_path),
        )
        save_state(
            state_path(cfg),
            _state(
                last_attempt_at=NOW - timedelta(hours=194),
                last_success_at=NOW - timedelta(hours=2),
                last_snapshot_id="seed01",
            ),
        )
        stamp = (NOW - timedelta(hours=194)).isoformat().replace("+00:00", "Z")
        runner = FakeDeepRunner(f'[{{"short_id": "seed01", "time": "{stamp}"}}]')
        fake = FakeNotifier()
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        with structlog.testing.capture_logs() as logs:
            # No deep=True: the sidecar is unstamped after the seed (deep never
            # ran pre-success), so the FIRST hourly tick auto-runs the deep tier.
            code = run_deadman_check(
                cfg,
                alerter=SweepAlerter(notifier=fake),
                clock=_clock,
                runner_factory=_factory(runner),
                ping=_ping,
            )
        assert code == EXIT_OK
        assert fake.published == []
        assert pings == ["http://hc.local/p"]  # plain — never /fail
        assert runner.tags == ["brutex"]
        ok = [e for e in logs if e["event"] == "backup.deadman.deep_ok"]
        assert len(ok) == 1
        assert "matches stamped last_snapshot_id" in ok[0]["detail"]
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW

    def test_deep_long_run_idless_state_nearer_attempt_stamp_agrees(self, tmp_path: Path) -> None:
        """Round-3 MAJOR pin (time-fallback arm, 41 h run): an id-less state
        whose success stamp sits a RUN DURATION (40 h > deadman 30 h) from the
        remote snapshot time still agrees, because the gap is judged against
        the NEARER of last_success_at and last_attempt_at (≈ backup START)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(
            state_path(cfg),
            _state(
                last_attempt_at=NOW - timedelta(hours=41),
                last_success_at=NOW - timedelta(hours=1),
                last_snapshot_id=None,
            ),
        )
        stamp = (NOW - timedelta(hours=41)).isoformat().replace("+00:00", "Z")
        runner = FakeDeepRunner(f'[{{"short_id": "run41h", "time": "{stamp}"}}]')
        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(
                cfg,
                deep=True,
                alerter=SweepAlerter(resolver=lambda: None),
                clock=_clock,
                runner_factory=_factory(runner),
            )
        assert code == EXIT_OK
        assert any(e["event"] == "backup.deadman.deep_ok" for e in logs)
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW

    def test_deep_divergence_idless_attemptless_state_still_fires(self, tmp_path: Path) -> None:
        """The forged/id-less state fallback keeps its teeth: no snapshot id,
        no attempt stamp, remote latest 200 h away → still CRITICAL (since
        round 4 this shape trips the stamp-plausibility tier: with no attempt
        stamp the anchor is the remote snapshot time, 199 h before success)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(
            state_path(cfg),
            _state(last_attempt_at=None, last_snapshot_id=None),
        )
        code = run_deadman_check(
            cfg,
            deep=True,
            alerter=SweepAlerter(resolver=lambda: None),
            clock=_clock,
            runner_factory=_factory(FakeDeepRunner(_diverging_snapshots_json())),
        )
        assert code == EXIT_DEADMAN_BREACH

    def test_deep_bumped_success_forgery_kept_id_diverges(self, tmp_path: Path) -> None:
        """Round-4 MAJOR pin (the F21 keystone): the SIMPLEST state poisoning
        — bump last_success_at to 1 h ago while keeping the REAL old
        last_snapshot_id + last_attempt_at (both ~40 d old, no remote
        knowledge needed) — must read CRITICAL divergence, not the id-match
        AGREE the round-3 short-circuit alone would give: the success stamp
        post-dates the run anchor by ~959 h ≫ max_runtime_hours (48)."""
        cfg = _shell_cfg(
            tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL="http://hc.local/p", **_repo_env(tmp_path)
        )
        save_state(
            state_path(cfg),
            _state(
                last_attempt_at=NOW - timedelta(days=40),
                last_success_at=NOW - timedelta(hours=1),  # the forged bump
                last_snapshot_id="deadbeef",  # the REAL old id, kept
            ),
        )
        stamp = (NOW - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        runner = FakeDeepRunner(f'[{{"short_id": "deadbeef", "time": "{stamp}"}}]')
        fake = FakeNotifier()
        pings: list[str] = []

        def _ping(url: str) -> bool:
            pings.append(url)
            return True

        with structlog.testing.capture_logs() as logs:
            code = run_deadman_check(
                cfg,
                deep=True,
                alerter=SweepAlerter(notifier=fake),
                clock=_clock,
                runner_factory=_factory(runner),
                ping=_ping,
            )
        assert code == EXIT_DEADMAN_BREACH
        assert fake.published[0].dedupe_key == "backup-overdue:state_remote_divergence"
        assert pings == ["http://hc.local/p/fail"]
        diverged = [e for e in logs if e["event"] == "backup.deadman.remote_divergence"]
        assert len(diverged) == 1
        assert "plausible-stamp forgery" in diverged[0]["detail"]

    def test_deep_bumped_success_forgery_idless_old_attempt_diverges(self, tmp_path: Path) -> None:
        """Round-4 MAJOR pin (id-less variant): the same bump-only forgery
        with the id blanked but the real old last_attempt_at preserved must
        diverge too — pre-fix the nearer-stamp fallback read a 0.0 h gap
        (attempt ≈ remote snapshot time) and AGREED."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        state = _state(
            last_attempt_at=NOW - timedelta(days=40),
            last_success_at=NOW - timedelta(hours=1),
            last_snapshot_id=None,
        )
        stamp = (NOW - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        runner = cast(
            "ResticRunner", FakeDeepRunner(f'[{{"short_id": "deadbeef", "time": "{stamp}"}}]')
        )
        diverged, completed, detail = _deep_divergence(cfg, state, runner)
        assert diverged is True
        assert completed is True
        assert "plausible-stamp forgery" in detail

    def test_deep_gap_arm_attemptless_state_within_plausibility_diverges(
        self, tmp_path: Path
    ) -> None:
        """The id-less time-gap fallback still has its OWN teeth below the
        plausibility bound: attempt-less + id-less, success 34 h after the
        remote snapshot (< max_runtime 48 h, so tier 1 passes) but gap
        34 h > deadman 30 h → divergence via the time-gap arm."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        state = _state(
            last_attempt_at=None,
            last_success_at=NOW - timedelta(hours=1),
            last_snapshot_id=None,
        )
        stamp = (NOW - timedelta(hours=35)).isoformat().replace("+00:00", "Z")
        runner = cast(
            "ResticRunner", FakeDeepRunner(f'[{{"short_id": "run35h", "time": "{stamp}"}}]')
        )
        diverged, completed, detail = _deep_divergence(cfg, state, runner)
        assert diverged is True
        assert completed is True
        assert "diverges from remote latest" in detail

    def test_snapshot_ids_match_prefix_both_directions(self) -> None:
        """short_id (remote) vs full id (state) match either way; id-less never."""
        assert _snapshot_ids_match("seed01ffff", "seed01") is True  # remote short_id
        assert _snapshot_ids_match("ab12", "ab12ffff") is True  # remote full id
        assert _snapshot_ids_match("ab12", "ab12") is True
        assert _snapshot_ids_match("ab12", "old1") is False
        assert _snapshot_ids_match(None, "ab12") is False
        assert _snapshot_ids_match("", "ab12") is False

    def test_deep_future_last_deep_check_at_runs_deep_and_self_heals(self, tmp_path: Path) -> None:
        """Round-3 MINOR pin: a FUTURE-stamped last_deep_check_at (botched
        ~/.brutex restore / backward NTP step — the F14 vectors, the
        _should_alert guard's twin) must read as 'no prior deep check', not
        mute the daily forgery-detection lane until the wall clock catches up;
        a completed comparison restamps with the CURRENT clock (self-heal)."""
        cfg = _shell_cfg(tmp_path, **_repo_env(tmp_path))
        save_state(state_path(cfg), _state())
        save_deadman(deadman_path(cfg), DeadmanState(last_deep_check_at=NOW + timedelta(days=365)))
        runner = FakeDeepRunner(_agreeing_snapshots_json())
        code = run_deadman_check(cfg, clock=_clock, runner_factory=_factory(runner))
        assert code == EXIT_OK
        assert runner.tags == ["brutex"]  # deep RAN despite the future stamp
        assert load_deadman(deadman_path(cfg)).last_deep_check_at == NOW  # healed


@pytest.fixture(name="ping_server")
def _ping_server() -> Iterator[tuple[str, list[str]]]:
    """A live localhost http.server recording every GET path (the F18 fixture)."""
    paths: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silence the default stderr access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class TestDefaultPing:
    def test_ping_plain_on_fresh(self, tmp_path: Path, ping_server: tuple[str, list[str]]) -> None:
        """The REAL _default_ping fires the plain URL on a FRESH tick."""
        url, paths = ping_server
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL=f"{url}/ping")
        save_state(state_path(cfg), _state())
        code = run_deadman_check(cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock)
        assert code == EXIT_OK
        assert paths == ["/ping"]

    def test_ping_fail_suffix_on_breach_real_server(
        self, tmp_path: Path, ping_server: tuple[str, list[str]]
    ) -> None:
        url, paths = ping_server
        cfg = _shell_cfg(tmp_path, BRUTEX_BACKUP_HEALTHCHECK_URL=f"{url}/ping")
        save_state(state_path(cfg), _state(last_success_at=NOW - timedelta(hours=40)))
        code = run_deadman_check(cfg, alerter=SweepAlerter(resolver=lambda: None), clock=_clock)
        assert code == EXIT_DEADMAN_BREACH
        assert paths == ["/ping/fail"]

    def test_ping_success_returns_true(self, ping_server: tuple[str, list[str]]) -> None:
        url, paths = ping_server
        assert _default_ping(f"{url}/direct") is True
        assert paths == ["/direct"]

    def test_ping_error_swallowed(self) -> None:
        """A dead endpoint → False + ONE WARN, never a raise (F18 best-effort)."""
        with structlog.testing.capture_logs() as logs:
            assert _default_ping("http://127.0.0.1:9/unroutable") is False
        assert [entry["event"] for entry in logs] == ["backup.deadman.ping_failed"]

    def test_ping_non_http_scheme_refused(self) -> None:
        """file:// and friends are refused before urlopen (S310 guard)."""
        with structlog.testing.capture_logs() as logs:
            assert _default_ping("file:///etc/passwd") is False
        assert [entry["event"] for entry in logs] == ["backup.deadman.ping_failed"]

    def test_ping_failure_logs_host_never_full_url(self) -> None:
        """Round-1 review pin: the ping URL is a WRITE-capability token — a
        failed ping logs ONLY the host, never the full URL (SET/UNSET stance)."""
        secret = "secret-capability-token-uuid"
        with structlog.testing.capture_logs() as logs:
            assert _default_ping(f"http://127.0.0.1:9/{secret}") is False
        events = [entry for entry in logs if entry["event"] == "backup.deadman.ping_failed"]
        assert len(events) == 1
        assert events[0]["url_host"] == "127.0.0.1"
        assert all(secret not in str(value) for entry in logs for value in entry.values())

    def test_ping_refusal_logs_host_never_full_url(self) -> None:
        """The non-http refusal branch is redacted too."""
        with structlog.testing.capture_logs() as logs:
            assert _default_ping("gopher://hc.example/secret-token-xyz") is False
        events = [entry for entry in logs if entry["event"] == "backup.deadman.ping_failed"]
        assert len(events) == 1
        assert events[0]["url_host"] == "hc.example"
        assert all(
            "secret-token-xyz" not in str(value) for entry in logs for value in entry.values()
        )

    def test_ping_control_char_url_no_raise_token_redacted(self) -> None:
        """Round-4 MINOR pin: an interior control char (a line-wrapped plist
        URL) raises http.client.InvalidURL — NOT a URLError/OSError/ValueError
        subclass — whose str() embeds the full capability token; _default_ping
        must swallow it and log only the exception TYPE name."""
        secret = "SECRET-UUID-TOKEN"
        with structlog.testing.capture_logs() as logs:
            assert _default_ping(f"https://hc.example/{secret}\n-more") is False
        events = [entry for entry in logs if entry["event"] == "backup.deadman.ping_failed"]
        assert len(events) == 1
        assert events[0]["error"] == "InvalidURL"
        assert all(secret not in str(value) for entry in logs for value in entry.values())

    def test_ping_nonnumeric_port_url_no_raise(self) -> None:
        """The InvalidURL port-typo shape is swallowed too (never a raise)."""
        with structlog.testing.capture_logs() as logs:
            assert _default_ping("https://hc.example:bad_port/tok-abc") is False
        events = [entry for entry in logs if entry["event"] == "backup.deadman.ping_failed"]
        assert len(events) == 1
        assert events[0]["error"] == "InvalidURL"
        assert all("tok-abc" not in str(value) for entry in logs for value in entry.values())

    def test_ping_unparseable_url_host_none(self) -> None:
        """urlsplit ValueError (invalid IPv6 netloc) → url_host=None, still no raise."""
        with structlog.testing.capture_logs() as logs:
            assert _default_ping("http://[::1-bad/secret-abc") is False
        events = [entry for entry in logs if entry["event"] == "backup.deadman.ping_failed"]
        assert len(events) == 1
        assert events[0]["url_host"] is None
        assert all("secret-abc" not in str(value) for entry in logs for value in entry.values())


class TestDefaultClock:
    def test_default_utc_clock_sanity(self) -> None:
        """Executed for real, aware-UTC, sanity-bounded — no pragma."""
        stamp = _default_utc_clock()
        assert stamp.tzinfo is UTC
        assert 2026 <= stamp.year <= 2100

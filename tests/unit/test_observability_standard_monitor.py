"""Coverage tests for :mod:`brutex.observability.standard_monitor` —
the production composer that wires SLA + Watchdog + L3 replay +
Telegram into a single :class:`MonitorLoop`.

Per DEC-STANDARD-MONITOR-001 (MIGRATION entry #243). Targets 100%
line + 100% branch coverage. Telegram-publish paths are tested via
monkeypatching :func:`brutex.notifications.wiring.get_or_start_notifier`
+ injecting a fake notifier with a captured-event transport.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from brutex.observability import standard_monitor
from brutex.observability.monitor import MonitorLoop
from brutex.observability.sla import (
    SLAEvent,
    SLAPredicate,
    SLARule,
    SLASeverity,
    SLAState,
)
from brutex.observability.standard_monitor import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REPLAY_ACTION_FILTER,
    DEFAULT_REPLAY_EVERY_N_TICKS,
    DEFAULT_WATCHDOG_TIMEOUT,
    ENV_AUDIT_PATH,
    ENV_LIVE_SUBJECTS_FILE,
    ENV_POLL_INTERVAL,
    ENV_REPLAY_ACTION_FILTER,
    ENV_REPLAY_EVERY_N_TICKS,
    ENV_TELEGRAM_ENABLED,
    ENV_WATCHDOG_TIMEOUT,
    MonitorConfig,
    _make_replay_check,
    _make_sla_callback,
    _make_watchdog_callback,
    _parse_bool_env,
    _parse_float_env,
    _parse_int_env,
    _parse_path_env,
    _publish_telegram,
    compose_standard_monitor,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _make_sla_event() -> SLAEvent:
    return SLAEvent(
        rule_name="r1",
        severity=SLASeverity.CRITICAL,
        description="synthetic",
        observed_value=150.0,
        threshold=100.0,
        predicate=SLAPredicate.GREATER_THAN,
        new_state=SLAState.BREACH,
    )


def _make_worker_event() -> WorkerEvent:
    return WorkerEvent(
        worker_name="w1",
        new_state=WorkerState.HUNG,
        last_beat_seconds_ago=15.0,
        timeout_seconds=10.0,
    )


# ────────────────────────────────────────────────────────────────────────
# Env-parsing helpers
# ────────────────────────────────────────────────────────────────────────


class TestParseFloatEnv:
    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TEST_FLOAT", raising=False)
        assert _parse_float_env("BRUTEX_TEST_FLOAT", 7.5) == 7.5

    def test_parses_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_FLOAT", "42.25")
        assert _parse_float_env("BRUTEX_TEST_FLOAT", 0.0) == 42.25

    def test_falls_back_to_default_on_bad_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("BRUTEX_TEST_FLOAT", "not-a-float")
        with caplog.at_level(logging.WARNING, logger="brutex.observability.standard_monitor"):
            assert _parse_float_env("BRUTEX_TEST_FLOAT", 99.0) == 99.0
        assert "not a float" in caplog.text


class TestParseIntEnv:
    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TEST_INT", raising=False)
        assert _parse_int_env("BRUTEX_TEST_INT", 3) == 3

    def test_parses_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_INT", "7")
        assert _parse_int_env("BRUTEX_TEST_INT", 0) == 7

    def test_falls_back_to_default_on_bad_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("BRUTEX_TEST_INT", "not-an-int")
        with caplog.at_level(logging.WARNING, logger="brutex.observability.standard_monitor"):
            assert _parse_int_env("BRUTEX_TEST_INT", 5) == 5
        assert "not an int" in caplog.text


class TestParseBoolEnv:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TEST_BOOL", raising=False)
        assert _parse_bool_env("BRUTEX_TEST_BOOL", default=False) is False
        assert _parse_bool_env("BRUTEX_TEST_BOOL", default=True) is True

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", " true ", "tRuE"])
    def test_true_variants_activate(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("BRUTEX_TEST_BOOL", value)
        assert _parse_bool_env("BRUTEX_TEST_BOOL", default=False) is True

    @pytest.mark.parametrize("value", ["1", "yes", "Y", "on", "0", "no", "false", ""])
    def test_non_true_returns_false_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Strict decoder semantics: ENV WINS when set. Non-true values
        return False explicitly regardless of default."""
        monkeypatch.setenv("BRUTEX_TEST_BOOL", value)
        assert _parse_bool_env("BRUTEX_TEST_BOOL", default=False) is False
        assert _parse_bool_env("BRUTEX_TEST_BOOL", default=True) is False


class TestParsePathEnv:
    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TEST_PATH", raising=False)
        assert _parse_path_env("BRUTEX_TEST_PATH") is None

    def test_returns_none_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_PATH", "")
        assert _parse_path_env("BRUTEX_TEST_PATH") is None

    def test_returns_none_when_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_PATH", "   ")
        assert _parse_path_env("BRUTEX_TEST_PATH") is None

    def test_returns_path_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_PATH", "/tmp/foo.txt")
        assert _parse_path_env("BRUTEX_TEST_PATH") == Path("/tmp/foo.txt")

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TEST_PATH", "  /tmp/foo.txt  ")
        assert _parse_path_env("BRUTEX_TEST_PATH") == Path("/tmp/foo.txt")


# ────────────────────────────────────────────────────────────────────────
# MonitorConfig
# ────────────────────────────────────────────────────────────────────────


class TestMonitorConfig:
    def test_defaults(self) -> None:
        cfg = MonitorConfig()
        assert cfg.poll_interval_seconds == DEFAULT_POLL_INTERVAL
        assert cfg.watchdog_timeout_seconds == DEFAULT_WATCHDOG_TIMEOUT
        assert cfg.telegram_enabled is False
        assert tuple(cfg.sla_rules) == ()
        assert cfg.audit_path is None
        assert cfg.live_subjects_file is None
        assert cfg.replay_action_filter == DEFAULT_REPLAY_ACTION_FILTER
        assert cfg.replay_every_n_ticks == DEFAULT_REPLAY_EVERY_N_TICKS

    def test_from_env_with_all_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_var in (
            ENV_POLL_INTERVAL,
            ENV_WATCHDOG_TIMEOUT,
            ENV_TELEGRAM_ENABLED,
            ENV_AUDIT_PATH,
            ENV_LIVE_SUBJECTS_FILE,
            ENV_REPLAY_ACTION_FILTER,
            ENV_REPLAY_EVERY_N_TICKS,
        ):
            monkeypatch.delenv(env_var, raising=False)
        cfg = MonitorConfig.from_env()
        assert cfg == MonitorConfig()

    def test_from_env_reads_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_POLL_INTERVAL, "5.0")
        monkeypatch.setenv(ENV_WATCHDOG_TIMEOUT, "60.0")
        monkeypatch.setenv(ENV_TELEGRAM_ENABLED, "true")
        monkeypatch.setenv(ENV_AUDIT_PATH, "/tmp/audit.jsonl")
        monkeypatch.setenv(ENV_LIVE_SUBJECTS_FILE, "/tmp/live.txt")
        monkeypatch.setenv(ENV_REPLAY_ACTION_FILTER, "custom")
        monkeypatch.setenv(ENV_REPLAY_EVERY_N_TICKS, "5")
        cfg = MonitorConfig.from_env()
        assert cfg.poll_interval_seconds == 5.0
        assert cfg.watchdog_timeout_seconds == 60.0
        assert cfg.telegram_enabled is True
        assert cfg.audit_path == Path("/tmp/audit.jsonl")
        assert cfg.live_subjects_file == Path("/tmp/live.txt")
        assert cfg.replay_action_filter == "custom"
        assert cfg.replay_every_n_ticks == 5

    def test_from_env_accepts_explicit_sla_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rule = SLARule(
            name="r1",
            value_provider=lambda: 1.0,
            predicate=SLAPredicate.GREATER_THAN,
            threshold=0.0,
        )
        cfg = MonitorConfig.from_env(sla_rules=(rule,))
        assert tuple(cfg.sla_rules) == (rule,)


# ────────────────────────────────────────────────────────────────────────
# compose_standard_monitor
# ────────────────────────────────────────────────────────────────────────


class TestComposeStandardMonitor:
    def test_returns_monitor_loop(self) -> None:
        loop = compose_standard_monitor(MonitorConfig())
        assert isinstance(loop, MonitorLoop)
        assert loop.sla_detector is not None
        assert loop.watchdog is not None
        assert loop.replay_check is None  # no audit_path / live_subjects_file
        assert loop.poll_interval_seconds == DEFAULT_POLL_INTERVAL

    def test_falls_through_to_env_when_no_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_POLL_INTERVAL, "1.0")
        loop = compose_standard_monitor()
        assert loop.poll_interval_seconds == 1.0

    def test_replay_check_wired_when_both_paths_set(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        live = tmp_path / "live.txt"
        cfg = MonitorConfig(
            audit_path=audit,
            live_subjects_file=live,
        )
        loop = compose_standard_monitor(cfg)
        assert loop.replay_check is not None

    def test_replay_check_none_when_only_audit_set(self, tmp_path: Path) -> None:
        cfg = MonitorConfig(audit_path=tmp_path / "audit.jsonl")
        loop = compose_standard_monitor(cfg)
        assert loop.replay_check is None

    def test_replay_check_none_when_only_live_set(self, tmp_path: Path) -> None:
        cfg = MonitorConfig(live_subjects_file=tmp_path / "live.txt")
        loop = compose_standard_monitor(cfg)
        assert loop.replay_check is None

    def test_propagates_replay_every_n_ticks(self) -> None:
        cfg = MonitorConfig(replay_every_n_ticks=5)
        loop = compose_standard_monitor(cfg)
        assert loop.replay_check_every_n_ticks == 5

    def test_sla_rules_propagate_into_detector(self) -> None:
        rule = SLARule(
            name="r1",
            value_provider=lambda: 1.0,
            predicate=SLAPredicate.GREATER_THAN,
            threshold=0.0,
        )
        cfg = MonitorConfig(sla_rules=(rule,))
        loop = compose_standard_monitor(cfg)
        assert loop.sla_detector is not None
        assert loop.sla_detector.rules == (rule,)


# ────────────────────────────────────────────────────────────────────────
# SLA + Watchdog callbacks
# ────────────────────────────────────────────────────────────────────────


class TestSLACallback:
    def test_logs_when_telegram_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = _make_sla_callback(telegram_enabled=False)
        with caplog.at_level(logging.INFO, logger="brutex.observability.standard_monitor"):
            cb(_make_sla_event())
        assert "sla_callback" in caplog.text
        assert "r1" in caplog.text

    def test_attempts_telegram_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[Any] = []

        class _FakeNotifier:
            def publish(self, event: Any) -> None:
                captured.append(event)

        monkeypatch.setattr(
            "brutex.notifications.wiring.get_or_start_notifier",
            _FakeNotifier,
        )
        cb = _make_sla_callback(telegram_enabled=True)
        cb(_make_sla_event())
        assert len(captured) == 1
        assert captured[0].kind == "phase.blocked"
        assert captured[0].dedupe_key == "r1"


class TestWatchdogCallback:
    def test_logs_when_telegram_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        cb = _make_watchdog_callback(telegram_enabled=False)
        with caplog.at_level(logging.INFO, logger="brutex.observability.standard_monitor"):
            cb(_make_worker_event())
        assert "watchdog_callback" in caplog.text
        assert "w1" in caplog.text

    def test_attempts_telegram_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[Any] = []

        class _FakeNotifier:
            def publish(self, event: Any) -> None:
                captured.append(event)

        monkeypatch.setattr(
            "brutex.notifications.wiring.get_or_start_notifier",
            _FakeNotifier,
        )
        cb = _make_watchdog_callback(telegram_enabled=True)
        cb(_make_worker_event())
        assert len(captured) == 1
        assert captured[0].kind == "worker.hung"
        assert captured[0].dedupe_key == "w1"


# ────────────────────────────────────────────────────────────────────────
# _publish_telegram
# ────────────────────────────────────────────────────────────────────────


class TestPublishTelegram:
    def test_silent_when_notifier_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brutex.notifications.wiring.get_or_start_notifier",
            lambda: None,
        )
        # Should not raise
        _publish_telegram(kind="x", severity="info", message="m", dedupe_key="d")

    def test_publishes_when_notifier_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[Any] = []

        class _FakeNotifier:
            def publish(self, event: Any) -> None:
                captured.append(event)

        monkeypatch.setattr(
            "brutex.notifications.wiring.get_or_start_notifier",
            _FakeNotifier,
        )
        _publish_telegram(kind="x", severity="info", message="m", dedupe_key="d")
        assert len(captured) == 1
        assert captured[0].kind == "x"
        assert captured[0].severity == "info"
        assert captured[0].message == "m"
        assert captured[0].dedupe_key == "d"

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError, AttributeError])
    def test_publish_exception_swallowed(
        self,
        exc_cls: type[BaseException],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _BrokenNotifier:
            def publish(self, _event: Any) -> None:
                raise exc_cls("synthetic")

        monkeypatch.setattr(
            "brutex.notifications.wiring.get_or_start_notifier",
            _BrokenNotifier,
        )
        with caplog.at_level(logging.ERROR, logger="brutex.observability.standard_monitor"):
            _publish_telegram(kind="x", severity="info", message="m", dedupe_key="d")
        assert "Telegram publish failed" in caplog.text

    def test_import_error_swallowed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If brutex.notifications import path is broken at runtime,
        the publish is silently dropped (Golden Rule #10)."""
        import builtins

        real_import = builtins.__import__

        def _broken_import(
            name: str,
            globals_: Any = None,
            locals_: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            if name.startswith("brutex.notifications"):
                raise ImportError(f"synthetic ImportError for {name}")
            return real_import(name, globals_, locals_, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _broken_import)
        with caplog.at_level(logging.ERROR, logger="brutex.observability.standard_monitor"):
            _publish_telegram(kind="x", severity="info", message="m", dedupe_key="d")
        assert "failed to import" in caplog.text


# ────────────────────────────────────────────────────────────────────────
# _make_replay_check
# ────────────────────────────────────────────────────────────────────────


class TestMakeReplayCheck:
    def test_returns_none_when_audit_missing(self, tmp_path: Path) -> None:
        assert (
            _make_replay_check(
                audit_path=None,
                live_subjects_file=tmp_path / "live.txt",
                action_filter="add",
            )
            is None
        )

    def test_returns_none_when_live_missing(self, tmp_path: Path) -> None:
        assert (
            _make_replay_check(
                audit_path=tmp_path / "audit.jsonl",
                live_subjects_file=None,
                action_filter="add",
            )
            is None
        )

    def test_replay_skipped_when_live_file_absent(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        audit.touch()  # empty audit log
        live = tmp_path / "ghost.txt"  # does NOT exist
        check = _make_replay_check(audit_path=audit, live_subjects_file=live, action_filter="add")
        assert check is not None
        with caplog.at_level(logging.WARNING, logger="brutex.observability.standard_monitor"):
            check()  # must not raise even though file is absent
        assert "does not exist" in caplog.text

    def test_replay_runs_when_both_files_present(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import AuditWriter

        audit = tmp_path / "audit.jsonl"
        with AuditWriter.open(audit) as log:
            log.append(action="add", subject="subj-a", payload={})
            log.append(action="add", subject="subj-b", payload={})
        live = tmp_path / "live.txt"
        live.write_text("subj-a\nsubj-b\n", encoding="utf-8")
        check = _make_replay_check(audit_path=audit, live_subjects_file=live, action_filter="add")
        assert check is not None
        # Must not raise — replay matches
        check()

    def test_replay_strips_blank_lines(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import AuditWriter

        audit = tmp_path / "audit.jsonl"
        with AuditWriter.open(audit) as log:
            log.append(action="add", subject="subj-a", payload={})
        live = tmp_path / "live.txt"
        live.write_text("\n\n  \nsubj-a\n\n", encoding="utf-8")
        check = _make_replay_check(audit_path=audit, live_subjects_file=live, action_filter="add")
        assert check is not None
        check()  # blank lines stripped → match → no raise


# ────────────────────────────────────────────────────────────────────────
# __all__ contract
# ────────────────────────────────────────────────────────────────────────


class TestExports:
    def test_all_exports_resolve(self) -> None:
        for name in standard_monitor.__all__:
            assert hasattr(standard_monitor, name), f"__all__ lies about {name}"

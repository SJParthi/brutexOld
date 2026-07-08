# pyright: reportPrivateUsage=none
"""Unit tests for ``brutex.observability.logging``.

Pins:

* ``configure_logging`` runs without error in JSON + console modes.
* Idempotence: calling twice with identical args does NOT reset state.
* Different args: second call overrides the first (last-call-wins).
* Level translation: ``"DEBUG"`` → 10, ``"INFO"`` → 20, etc.
* Invalid level name raises ValueError.
* Logger output is valid JSON when json_renderer=True.
* Logger output includes timestamp + level + event keys.
* ``reset_configuration`` clears state for next test.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

import pytest
import structlog
from brutex.observability.logging import (
    _FILE_HANDLER_TAG,
    LOG_LEVEL_ENV,
    _level_to_int,
    configure_logging,
    configure_process_logging_best_effort,
    reset_configuration,
    resolve_env_log_level,
    resolve_log_file,
)


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Any:
    """Reset config + structlog defaults between tests."""
    reset_configuration()
    yield
    reset_configuration()


class TestConfigureLoggingSmoke:
    """The function runs without error in its 2 main modes."""

    def test_json_mode(self) -> None:
        configure_logging(level="INFO", json_renderer=True)

    def test_console_mode(self) -> None:
        configure_logging(level="INFO", json_renderer=False)

    def test_debug_level(self) -> None:
        configure_logging(level="DEBUG", json_renderer=True)


class TestIdempotence:
    """Calling configure_logging twice with same args = no-op the second time."""

    def test_identical_args_no_op(self) -> None:
        configure_logging(level="INFO", json_renderer=True)
        # Capture handler-id; should be unchanged on second identical call.
        handler_id_before = id(logging.getLogger().handlers[0])
        configure_logging(level="INFO", json_renderer=True)
        handler_id_after = id(logging.getLogger().handlers[0])
        assert handler_id_before == handler_id_after

    def test_different_level_overrides(self) -> None:
        configure_logging(level="INFO", json_renderer=True)
        configure_logging(level="DEBUG", json_renderer=True)
        # Root logger level should now be DEBUG (10).
        assert logging.getLogger().level == logging.DEBUG

    def test_different_renderer_overrides(self) -> None:
        configure_logging(level="INFO", json_renderer=True)
        configure_logging(level="INFO", json_renderer=False)
        # Handler is replaced; we can't easily assert renderer type
        # without poking internals, but at least re-config didn't crash.
        assert len(logging.getLogger().handlers) == 1


class TestLevelToInt:
    """Stdlib level-name translation."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("DEBUG", 10), ("INFO", 20), ("WARNING", 30), ("ERROR", 40), ("CRITICAL", 50)],
    )
    def test_known_names(self, name: str, expected: int) -> None:
        assert _level_to_int(name) == expected

    def test_lowercase_accepted(self) -> None:
        assert _level_to_int("debug") == 10

    def test_numeric_string_passthrough(self) -> None:
        assert _level_to_int("25") == 25

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unrecognised level name"):
            _level_to_int("VERBOSE")


class TestJSONOutput:
    """The JSON renderer emits parseable single-line JSON with standard keys."""

    def test_log_emits_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_renderer=True)
        log = structlog.get_logger("test")
        log.info("test_event", k1="v1", k2=42)

        captured = capsys.readouterr()
        # structlog's PrintLoggerFactory writes to stderr (configured).
        line = captured.err.strip().split("\n")[-1]
        payload = json.loads(line)
        assert payload["event"] == "test_event"
        assert payload["k1"] == "v1"
        assert payload["k2"] == 42
        assert payload["level"] == "info"
        assert "timestamp" in payload

    def test_log_below_level_suppressed(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", json_renderer=True)
        log = structlog.get_logger("test")
        log.info("should_not_appear", x=1)

        captured = capsys.readouterr()
        assert "should_not_appear" not in captured.err


class TestResetConfiguration:
    """Reset clears the idempotence guard."""

    def test_reset_allows_reconfigure(self) -> None:
        configure_logging(level="INFO", json_renderer=True)
        # Same args — would be no-op without reset.
        reset_configuration()
        configure_logging(level="INFO", json_renderer=True)
        # If we got here without error, reset path is exercised.
        assert len(logging.getLogger().handlers) == 1


class TestResolveEnvLogLevel:
    """$BRUTEX_LOG_LEVEL resolution — DEC-SWEEP-LOG-VOLUME-GUARD-001.

    The fail-loud env wrapper the module contract prescribes: unset/blank →
    default, the four canonical names accepted case-insensitively, anything
    else raises (never a silent fallback).
    """

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        assert resolve_env_log_level() == "INFO"

    def test_blank_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "   ")
        assert resolve_env_log_level() == "INFO"

    def test_custom_default_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        assert resolve_env_log_level(default="WARNING") == "WARNING"

    @pytest.mark.parametrize("name", ["DEBUG", "INFO", "WARNING", "ERROR"])
    def test_canonical_names_accepted(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, name)
        assert resolve_env_log_level() == name

    @pytest.mark.parametrize(("raw", "expected"), [("debug", "DEBUG"), ("  Error  ", "ERROR")])
    def test_case_insensitive_and_stripped(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, raw)
        assert resolve_env_log_level() == expected

    @pytest.mark.parametrize("junk", ["TRACE", "DEBG", "15", "CRITICAL"])
    def test_junk_raises_naming_the_var(self, monkeypatch: pytest.MonkeyPatch, junk: str) -> None:
        # CRITICAL is deliberately junk here: the env contract is the four
        # obvious words only (an operator silencing ERROR would hide halts).
        monkeypatch.setenv(LOG_LEVEL_ENV, junk)
        with pytest.raises(ValueError, match="invalid BRUTEX_LOG_LEVEL"):
            resolve_env_log_level()


class TestResolveLogFile:
    """Path resolution order: explicit → BRUTEX_LOG_FILE → BRUTEX_LOG_DIR → default."""

    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        target = tmp_path / "custom" / "x.log"
        assert resolve_log_file(target) == target

    def test_explicit_str_path_expanded(self) -> None:
        # ``~`` is expanded for a string argument too.
        out = resolve_log_file("~/somewhere/y.log")
        assert out == Path.home() / "somewhere" / "y.log"

    def test_env_log_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "envfile.log"
        monkeypatch.setenv("BRUTEX_LOG_FILE", str(target))
        monkeypatch.delenv("BRUTEX_LOG_DIR", raising=False)
        assert resolve_log_file() == target

    def test_env_log_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_LOG_FILE", raising=False)
        monkeypatch.setenv("BRUTEX_LOG_DIR", str(tmp_path))
        assert resolve_log_file() == tmp_path / "brutex.log"

    def test_default_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_LOG_FILE", raising=False)
        monkeypatch.delenv("BRUTEX_LOG_DIR", raising=False)
        assert resolve_log_file() == Path.home() / ".brutex" / "logs" / "brutex.log"


class TestFileSink:
    """The opt-in rotating file handler writes alongside the console."""

    def test_disabled_by_default(self) -> None:
        ret = configure_logging(level="INFO", json_renderer=True)
        assert ret is None
        handlers = logging.getLogger().handlers
        assert not any(getattr(h, _FILE_HANDLER_TAG, False) for h in handlers)

    def test_enable_file_adds_rotating_handler(self, tmp_path: Path) -> None:
        target = tmp_path / "logs" / "brutex.log"
        ret = configure_logging(level="INFO", json_renderer=True, log_file=target)
        assert ret == target
        # Parent dir was created.
        assert target.parent.is_dir()
        handlers = logging.getLogger().handlers
        file_handlers = [h for h in handlers if getattr(h, _FILE_HANDLER_TAG, False)]
        assert len(file_handlers) == 1
        assert isinstance(file_handlers[0], logging.handlers.RotatingFileHandler)
        # Console handler still present (console is not broken).
        assert len(handlers) == 2

    def test_enable_file_via_flag_uses_env_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_LOG_FILE", raising=False)
        monkeypatch.setenv("BRUTEX_LOG_DIR", str(tmp_path))
        ret = configure_logging(level="INFO", json_renderer=True, enable_file=True)
        assert ret == tmp_path / "brutex.log"

    def test_file_receives_json_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "brutex.log"
        configure_logging(level="INFO", json_renderer=False, log_file=target)
        log = structlog.get_logger("filetest")
        log.warning("file_event", code=7)
        # Flush handlers so the file is written.
        for h in logging.getLogger().handlers:
            h.flush()
        text = target.read_text(encoding="utf-8").strip()
        last = text.split("\n")[-1]
        payload = json.loads(last)  # file sink is always JSON even in console mode
        assert payload["event"] == "file_event"
        assert payload["code"] == 7
        assert payload["level"] == "warning"

    def test_event_logged_exactly_once_in_file(self, tmp_path: Path) -> None:
        """A single emit produces exactly ONE file line — guards the duplicate-line
        risk if a second file handler ever stacked (hostile-review NIT-2)."""
        target = tmp_path / "brutex.log"
        configure_logging(level="INFO", json_renderer=True, log_file=target)
        structlog.get_logger("oncetest").warning("once_event", code=42)
        for h in logging.getLogger().handlers:
            h.flush()
        matches = [
            ln for ln in target.read_text(encoding="utf-8").splitlines() if "once_event" in ln
        ]
        assert len(matches) == 1

    def test_repeated_calls_do_not_stack_file_handlers(self, tmp_path: Path) -> None:
        target = tmp_path / "brutex.log"
        configure_logging(level="INFO", json_renderer=True, log_file=target)
        reset_configuration()
        configure_logging(level="DEBUG", json_renderer=True, log_file=target)
        handlers = logging.getLogger().handlers
        file_handlers = [h for h in handlers if getattr(h, _FILE_HANDLER_TAG, False)]
        assert len(file_handlers) == 1

    def test_idempotent_with_file(self, tmp_path: Path) -> None:
        target = tmp_path / "brutex.log"
        configure_logging(level="INFO", json_renderer=True, log_file=target)
        before = id(logging.getLogger().handlers[0])
        # Identical args → no-op, returns resolved path without rebuilding.
        ret = configure_logging(level="INFO", json_renderer=True, log_file=target)
        assert ret == target
        after = id(logging.getLogger().handlers[0])
        assert before == after

    def test_custom_rotation_params(self, tmp_path: Path) -> None:
        target = tmp_path / "brutex.log"
        configure_logging(
            level="INFO",
            json_renderer=True,
            log_file=target,
            max_bytes=1024,
            backup_count=2,
        )
        handlers = logging.getLogger().handlers
        fh = next(h for h in handlers if getattr(h, _FILE_HANDLER_TAG, False))
        assert isinstance(fh, logging.handlers.RotatingFileHandler)
        assert fh.maxBytes == 1024
        assert fh.backupCount == 2


class TestConfigureProcessLoggingBestEffort:
    """The spawn-worker bound — DEC-SWEEP-LOG-VOLUME-GUARD-001 R4 fold.

    Best-effort by contract: junk env falls back to the INFO default (the
    parent entries already failed loud pre-pool), an existing configuration
    is deferred to, and ANY configuration failure is swallowed — the caller
    (a pool initializer) must never die over logging.
    """

    def test_default_binds_info_and_suppresses_debug(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        assert not structlog.is_configured()
        configure_process_logging_best_effort()
        assert structlog.is_configured()
        log = structlog.get_logger("worker")
        log.debug("trade_sim.entry", idx=1)
        log.warning("sweep.quarantine", symbol="NIFTY")
        err = capsys.readouterr().err
        assert "trade_sim.entry" not in err
        assert "sweep.quarantine" in err

    def test_debug_env_binds_debug(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
        configure_process_logging_best_effort()
        structlog.get_logger("worker").debug("trade_sim.exit", pnl_paisa=1)
        assert "trade_sim.exit" in capsys.readouterr().err

    def test_junk_env_falls_back_to_info_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBG")
        configure_process_logging_best_effort()  # must NOT raise
        assert structlog.is_configured()
        structlog.get_logger("worker").debug("should_not_appear")
        assert "should_not_appear" not in capsys.readouterr().err

    def test_defers_to_existing_configuration(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="DEBUG", json_renderer=False)
        configure_process_logging_best_effort()  # would bind INFO if it did not defer
        structlog.get_logger("worker").debug("still_debug_bound")
        assert "still_debug_bound" in capsys.readouterr().err

    def test_config_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_kw: object) -> None:
            raise RuntimeError("configure blew up")

        monkeypatch.setattr("brutex.observability.logging.configure_logging", _boom)
        configure_process_logging_best_effort()  # must NOT raise

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
from typing import Any

import pytest
import structlog
from brutex.observability.logging import (
    _level_to_int,
    configure_logging,
    reset_configuration,
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

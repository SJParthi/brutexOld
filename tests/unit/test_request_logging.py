"""Unit tests for `brutex.dashboard.request_logging` — DEC-DASHBOARD-REQUEST-LOGGING-001.

100% line+branch: the high-frequency poll classifier (both arms), the uvicorn
access-log silencer (default + custom level), and the per-request emitter (poll
→ DEBUG vs non-poll → INFO, field shape, latency rounding, ``client=None``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from brutex.dashboard.request_logging import (
    HIGH_FREQUENCY_POLL_PATHS,
    emit_request_log,
    is_high_frequency_poll,
    silence_uvicorn_access_log,
)
from structlog.testing import capture_logs


@pytest.fixture
def _restore_uvicorn_access_level() -> Iterator[None]:
    """Restore the uvicorn.access logger level after a silencer test."""
    log = logging.getLogger("uvicorn.access")
    original = log.level
    try:
        yield
    finally:
        log.setLevel(original)


@pytest.mark.parametrize("path", sorted(HIGH_FREQUENCY_POLL_PATHS))
def test_high_frequency_poll_paths_classify_as_polls(path: str) -> None:
    assert is_high_frequency_poll(path) is True


@pytest.mark.parametrize("path", ["/api/v1/state", "/db", "/api/v1/db/tables", "/", "/coverage"])
def test_non_poll_paths_do_not_classify_as_polls(path: str) -> None:
    assert is_high_frequency_poll(path) is False


@pytest.mark.usefixtures("_restore_uvicorn_access_level")
def test_silence_uvicorn_access_log_defaults_to_warning() -> None:
    silence_uvicorn_access_log()
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


@pytest.mark.usefixtures("_restore_uvicorn_access_level")
def test_silence_uvicorn_access_log_honours_custom_level() -> None:
    silence_uvicorn_access_log(logging.ERROR)
    assert logging.getLogger("uvicorn.access").level == logging.ERROR


def test_emit_request_log_non_poll_is_info_with_rounded_fields() -> None:
    with capture_logs() as logs:
        emit_request_log(
            method="GET",
            path="/api/v1/state",
            status_code=200,
            latency_ms=1.23456,
            request_id="abc123abc123",
            client="127.0.0.1",
        )
    assert len(logs) == 1
    entry = logs[0]
    assert entry["event"] == "dashboard.request"
    assert entry["log_level"] == "info"
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/v1/state"
    assert entry["status_code"] == 200
    assert entry["latency_ms"] == 1.23  # rounded to 2 dp for readability
    assert entry["request_id"] == "abc123abc123"
    assert entry["client"] == "127.0.0.1"


def test_emit_request_log_poll_path_is_debug_and_allows_null_client() -> None:
    with capture_logs() as logs:
        emit_request_log(
            method="GET",
            path="/api/v1/universe/pull-progress",
            status_code=200,
            latency_ms=0.5,
            request_id="def456def456",
            client=None,
        )
    assert len(logs) == 1
    entry = logs[0]
    assert entry["event"] == "dashboard.request"
    assert entry["log_level"] == "debug"
    assert entry["client"] is None

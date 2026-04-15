"""Unit tests for brutex.config.runtime — service discovery file."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.config.runtime import (
    ENV_RUNTIME_FILE,
    RuntimeFileError,
    RuntimeInfo,
    _redact_env_snapshot,
    build_runtime_info,
    clear_stale_runtime,
    delete_runtime_file,
    is_pid_alive,
    read_runtime_file,
    runtime_file_path,
    write_runtime_file,
)

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_runtime_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the runtime file to a temp location."""
    path = tmp_path / "runtime.json"
    monkeypatch.setenv(ENV_RUNTIME_FILE, str(path))
    return path


@pytest.fixture
def sample_info() -> RuntimeInfo:
    return RuntimeInfo(
        pid=12345,
        run_id="test-run-abc123",
        started_at="2026-04-14T12:00:00Z",
        host_bind="0.0.0.0",
        dashboard_urls=("http://192.168.1.42:9080/", "http://localhost:9080/"),
        metrics_url="http://localhost:9081/metrics",
        phase="A_INSTRUMENTS",
        env_snapshot=(
            ("BRUTEX_OHLC_RATE_LIMIT_RPS", "15"),
            ("BRUTEX_PORT_DASHBOARD", "9080"),
        ),
        environment="terminal",
    )


# ────────────────────────────────────────────────────────────────────────
# runtime_file_path
# ────────────────────────────────────────────────────────────────────────


class TestRuntimeFilePath:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_RUNTIME_FILE, raising=False)
        path = runtime_file_path()
        assert path.name == "runtime.json"
        assert path.parent.name == ".brutex"

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RUNTIME_FILE, "/tmp/custom.json")
        assert runtime_file_path() == Path("/tmp/custom.json")

    def test_override_expands_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RUNTIME_FILE, "~/custom.json")
        result = runtime_file_path()
        # ~ should be expanded to the user's home dir
        assert "~" not in str(result)


# ────────────────────────────────────────────────────────────────────────
# RuntimeInfo serialisation
# ────────────────────────────────────────────────────────────────────────


class TestRuntimeInfoSerialization:
    def test_to_json_round_trip(self, sample_info: RuntimeInfo) -> None:
        raw = sample_info.to_json()
        parsed = RuntimeInfo.from_json(raw)
        assert parsed == sample_info

    def test_to_json_is_sorted(self, sample_info: RuntimeInfo) -> None:
        raw = sample_info.to_json()
        data = json.loads(raw)
        keys = list(data.keys())
        assert keys == sorted(keys)

    def test_from_json_missing_field_raises(self) -> None:
        incomplete = json.dumps({"pid": 1, "run_id": "x"})
        with pytest.raises(RuntimeFileError, match="missing required fields"):
            RuntimeInfo.from_json(incomplete)

    def test_from_json_malformed_raises(self) -> None:
        with pytest.raises(RuntimeFileError, match="not valid JSON"):
            RuntimeInfo.from_json("{this is not json")


# ────────────────────────────────────────────────────────────────────────
# _redact_env_snapshot
# ────────────────────────────────────────────────────────────────────────


class TestRedactEnvSnapshot:
    def test_non_brutex_vars_dropped(self) -> None:
        result = _redact_env_snapshot({"PATH": "/usr/bin", "HOME": "/home/u"})
        assert result == {}

    def test_brutex_vars_kept(self) -> None:
        result = _redact_env_snapshot(
            {
                "BRUTEX_PORT_DASHBOARD": "9080",
                "PATH": "/usr/bin",
            }
        )
        assert result == {"BRUTEX_PORT_DASHBOARD": "9080"}

    def test_token_redacted(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_TELEGRAM_BOT_TOKEN": "secret:stuff"})
        assert result == {"BRUTEX_TELEGRAM_BOT_TOKEN": "<redacted>"}

    def test_secret_redacted(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_DB_SECRET": "abc123"})
        assert result == {"BRUTEX_DB_SECRET": "<redacted>"}

    def test_password_redacted(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_X_PASSWORD": "pw"})
        assert result == {"BRUTEX_X_PASSWORD": "<redacted>"}

    def test_api_key_redacted(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_GROWW_API_KEY": "key"})
        assert result == {"BRUTEX_GROWW_API_KEY": "<redacted>"}

    def test_auth_redacted(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_GROWW_API_AUTH_TOKEN": "t"})
        # Matches both AUTH and TOKEN patterns
        assert result == {"BRUTEX_GROWW_API_AUTH_TOKEN": "<redacted>"}

    def test_case_insensitive(self) -> None:
        result = _redact_env_snapshot({"BRUTEX_x_token_y": "v"})
        assert result == {"BRUTEX_x_token_y": "<redacted>"}


# ────────────────────────────────────────────────────────────────────────
# build_runtime_info
# ────────────────────────────────────────────────────────────────────────


class TestBuildRuntimeInfo:
    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_FOO", "bar")
        info = build_runtime_info(
            pid=123,
            run_id="r1",
            host_bind="0.0.0.0",
            dashboard_urls=["http://localhost:9080/"],
            metrics_url="http://localhost:9081/metrics",
            phase="idle",
            environment="terminal",
            started_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        )
        assert info.pid == 123
        assert info.started_at == "2026-01-01T12:00:00Z"
        assert ("BRUTEX_FOO", "bar") in info.env_snapshot

    def test_env_snapshot_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_Z", "1")
        monkeypatch.setenv("BRUTEX_A", "2")
        info = build_runtime_info(
            pid=1,
            run_id="r",
            host_bind="0.0.0.0",
            dashboard_urls=["http://localhost:9080/"],
            metrics_url="http://localhost:9081/metrics",
            phase="idle",
            environment="terminal",
        )
        keys = [k for k, _ in info.env_snapshot]
        assert keys == sorted(keys)

    def test_secret_env_vars_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TELEGRAM_BOT_TOKEN", "SECRETVALUE")
        info = build_runtime_info(
            pid=1,
            run_id="r",
            host_bind="0.0.0.0",
            dashboard_urls=["http://localhost:9080/"],
            metrics_url="http://localhost:9081/metrics",
            phase="idle",
            environment="terminal",
        )
        snapshot = dict(info.env_snapshot)
        assert snapshot.get("BRUTEX_TELEGRAM_BOT_TOKEN") == "<redacted>"
        assert "SECRETVALUE" not in info.to_json()


# ────────────────────────────────────────────────────────────────────────
# File I/O
# ────────────────────────────────────────────────────────────────────────


class TestWriteReadRoundTrip:
    def test_write_then_read(self, tmp_runtime_file: Path, sample_info: RuntimeInfo) -> None:
        path = write_runtime_file(sample_info)
        assert path == tmp_runtime_file
        assert path.exists()

        info = read_runtime_file()
        assert info == sample_info

    def test_read_missing_file_returns_none(self, tmp_runtime_file: Path) -> None:
        assert not tmp_runtime_file.exists()
        assert read_runtime_file() is None

    def test_write_creates_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_info: RuntimeInfo
    ) -> None:
        deep = tmp_path / "nested" / "path" / "runtime.json"
        monkeypatch.setenv(ENV_RUNTIME_FILE, str(deep))
        write_runtime_file(sample_info)
        assert deep.exists()
        assert deep.parent.is_dir()

    def test_write_is_atomic(self, tmp_runtime_file: Path, sample_info: RuntimeInfo) -> None:
        """Verify no temp file is left behind after a normal write."""
        write_runtime_file(sample_info)
        siblings = list(tmp_runtime_file.parent.iterdir())
        # Only the target file should exist, no .runtime.*.tmp files
        temp_files = [f for f in siblings if f.name.startswith(".runtime.")]
        assert temp_files == []

    def test_delete_existing(self, tmp_runtime_file: Path, sample_info: RuntimeInfo) -> None:
        write_runtime_file(sample_info)
        assert delete_runtime_file() is True
        assert not tmp_runtime_file.exists()

    def test_delete_missing_returns_false(self, tmp_runtime_file: Path) -> None:
        assert delete_runtime_file() is False


# ────────────────────────────────────────────────────────────────────────
# is_pid_alive
# ────────────────────────────────────────────────────────────────────────


class TestIsPidAlive:
    def test_self_pid_is_alive(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_dead_pid_returns_false(self) -> None:
        """PID 2**31 - 1 is almost certainly not allocated."""
        assert is_pid_alive(2**31 - 1) is False

    def test_pid_zero_returns_false(self) -> None:
        """PID 0 is the idle process / invalid — reject."""
        assert is_pid_alive(0) is False

    def test_negative_pid_returns_false(self) -> None:
        assert is_pid_alive(-1) is False


# ────────────────────────────────────────────────────────────────────────
# clear_stale_runtime
# ────────────────────────────────────────────────────────────────────────


class TestClearStaleRuntime:
    def test_no_file_returns_false(self, tmp_runtime_file: Path) -> None:
        assert clear_stale_runtime() is False

    def test_alive_pid_returns_false(
        self, tmp_runtime_file: Path, sample_info: RuntimeInfo
    ) -> None:
        """Runtime file with the current process's PID → not stale."""
        alive_info = RuntimeInfo(
            pid=os.getpid(),
            run_id=sample_info.run_id,
            started_at=sample_info.started_at,
            host_bind=sample_info.host_bind,
            dashboard_urls=sample_info.dashboard_urls,
            metrics_url=sample_info.metrics_url,
            phase=sample_info.phase,
            env_snapshot=sample_info.env_snapshot,
            environment=sample_info.environment,
        )
        write_runtime_file(alive_info)
        assert clear_stale_runtime() is False
        assert tmp_runtime_file.exists()

    def test_dead_pid_clears_file(self, tmp_runtime_file: Path, sample_info: RuntimeInfo) -> None:
        """Runtime file with an impossible PID → cleared."""
        dead_info = RuntimeInfo(
            pid=2**31 - 1,
            run_id=sample_info.run_id,
            started_at=sample_info.started_at,
            host_bind=sample_info.host_bind,
            dashboard_urls=sample_info.dashboard_urls,
            metrics_url=sample_info.metrics_url,
            phase=sample_info.phase,
            env_snapshot=sample_info.env_snapshot,
            environment=sample_info.environment,
        )
        write_runtime_file(dead_info)
        assert clear_stale_runtime() is True
        assert not tmp_runtime_file.exists()

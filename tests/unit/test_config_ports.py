"""Unit tests for brutex.config.ports.

Covers:

* Default values match NFR-PORT-004
* Env var overrides at all precedence levels
* Invalid values rejected with InvalidPortError
* Tickvault-reserved ports are detected (is_tickvault_reserved_port)
* PortInUseError message includes the right hints
* PortConfig.dashboard_urls returns correct forms per environment
* detect_environment picks the right RuntimeEnvironment
* check_port_free / assert_port_available round-trip
"""

from __future__ import annotations

import errno
import os
import socket
from unittest.mock import patch

import pytest

from brutex.config.ports import (
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_HOST_BIND,
    DEFAULT_JUPYTER_PORT,
    DEFAULT_METRICS_PORT,
    DEFAULT_RAY_DASHBOARD_PORT,
    ENV_HOST_BIND,
    ENV_PORT_DASHBOARD,
    ENV_PORT_JUPYTER,
    ENV_PORT_METRICS,
    ENV_PORT_RAY,
    TICKVAULT_RESERVED_PORTS,
    InvalidPortError,
    PortConfig,
    PortInUseError,
    RuntimeEnvironment,
    assert_port_available,
    check_port_free,
    detect_environment,
    detect_lan_ip,
    is_tickvault_reserved_port,
    load_port_config,
)


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear every BRUTEX_* env var and every env-detection var."""
    for key in list(os.environ):
        if key.startswith("BRUTEX_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE",
        "CLAUDECODE",
        "PYCHARM_HOSTED",
        "IDEA_INITIAL_DIRECTORY",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ────────────────────────────────────────────────────────────────────────
# Default values
# ────────────────────────────────────────────────────────────────────────


class TestDefaults:
    def test_dashboard_is_9080(self) -> None:
        assert DEFAULT_DASHBOARD_PORT == 9080

    def test_metrics_is_9081_not_9090(self) -> None:
        """The critical fact-check: 9090 would collide with tv-prometheus."""
        assert DEFAULT_METRICS_PORT == 9081
        assert DEFAULT_METRICS_PORT != 9090, (
            "9090 is tv-prometheus per tickvault CLAUDE.md — see NFR-PORT-004"
        )

    def test_jupyter_is_9888(self) -> None:
        assert DEFAULT_JUPYTER_PORT == 9888

    def test_ray_is_9265(self) -> None:
        assert DEFAULT_RAY_DASHBOARD_PORT == 9265

    def test_host_bind_is_0_0_0_0(self) -> None:
        """Default binds to all interfaces for LAN access (phone, tablet)."""
        assert DEFAULT_HOST_BIND == "0.0.0.0"

    def test_all_defaults_in_9xxx_range(self) -> None:
        """Sanity: every BruteX default lives in 9000-9999."""
        for port in (
            DEFAULT_DASHBOARD_PORT,
            DEFAULT_METRICS_PORT,
            DEFAULT_JUPYTER_PORT,
            DEFAULT_RAY_DASHBOARD_PORT,
        ):
            assert 9000 <= port <= 9999

    def test_no_default_is_tickvault_reserved(self) -> None:
        """Every BruteX default must avoid every tickvault port."""
        for port in (
            DEFAULT_DASHBOARD_PORT,
            DEFAULT_METRICS_PORT,
            DEFAULT_JUPYTER_PORT,
            DEFAULT_RAY_DASHBOARD_PORT,
        ):
            assert not is_tickvault_reserved_port(port), (
                f"Default port {port} collides with a tickvault service — "
                f"see TICKVAULT_RESERVED_PORTS"
            )


# ────────────────────────────────────────────────────────────────────────
# Tickvault reserved port set
# ────────────────────────────────────────────────────────────────────────


class TestTickvaultReservedPorts:
    def test_prometheus_9090_reserved(self) -> None:
        assert 9090 in TICKVAULT_RESERVED_PORTS
        assert is_tickvault_reserved_port(9090) is True

    def test_questdb_9000_9009_reserved(self) -> None:
        assert 9000 in TICKVAULT_RESERVED_PORTS
        assert 9009 in TICKVAULT_RESERVED_PORTS

    def test_traefik_8080_reserved(self) -> None:
        assert 8080 in TICKVAULT_RESERVED_PORTS

    def test_valkey_6379_reserved(self) -> None:
        assert 6379 in TICKVAULT_RESERVED_PORTS

    def test_grafana_3000_reserved(self) -> None:
        assert 3000 in TICKVAULT_RESERVED_PORTS

    def test_api_3001_reserved(self) -> None:
        assert 3001 in TICKVAULT_RESERVED_PORTS

    def test_loki_3100_reserved(self) -> None:
        assert 3100 in TICKVAULT_RESERVED_PORTS

    def test_jaeger_16686_reserved(self) -> None:
        assert 16686 in TICKVAULT_RESERVED_PORTS

    def test_non_reserved_ports(self) -> None:
        assert is_tickvault_reserved_port(9080) is False
        assert is_tickvault_reserved_port(9081) is False
        assert is_tickvault_reserved_port(9888) is False

    def test_reserved_set_is_frozen(self) -> None:
        assert isinstance(TICKVAULT_RESERVED_PORTS, frozenset)


# ────────────────────────────────────────────────────────────────────────
# load_port_config — env var parsing
# ────────────────────────────────────────────────────────────────────────


class TestLoadPortConfig:
    def test_defaults_with_no_env(self, clean_env: pytest.MonkeyPatch) -> None:
        cfg = load_port_config()
        assert cfg.dashboard == DEFAULT_DASHBOARD_PORT
        assert cfg.metrics == DEFAULT_METRICS_PORT
        assert cfg.jupyter == DEFAULT_JUPYTER_PORT
        assert cfg.ray_dashboard == DEFAULT_RAY_DASHBOARD_PORT
        assert cfg.host_bind == DEFAULT_HOST_BIND
        assert cfg.environment == RuntimeEnvironment.TERMINAL

    def test_env_override_dashboard(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "9180")
        cfg = load_port_config()
        assert cfg.dashboard == 9180

    def test_env_override_metrics(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_METRICS, "9181")
        cfg = load_port_config()
        assert cfg.metrics == 9181

    def test_env_override_host_bind(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_HOST_BIND, "127.0.0.1")
        cfg = load_port_config()
        assert cfg.host_bind == "127.0.0.1"

    def test_env_override_multiple(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "9180")
        clean_env.setenv(ENV_PORT_METRICS, "9181")
        clean_env.setenv(ENV_PORT_JUPYTER, "9988")
        clean_env.setenv(ENV_PORT_RAY, "9365")
        cfg = load_port_config()
        assert cfg.dashboard == 9180
        assert cfg.metrics == 9181
        assert cfg.jupyter == 9988
        assert cfg.ray_dashboard == 9365


class TestInvalidPort:
    def test_non_numeric_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "not-a-number")
        with pytest.raises(InvalidPortError, match="integer"):
            load_port_config()

    def test_negative_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "-5")
        with pytest.raises(InvalidPortError, match="1..65535"):
            load_port_config()

    def test_zero_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "0")
        with pytest.raises(InvalidPortError, match="1..65535"):
            load_port_config()

    def test_too_large_raises(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "65536")
        with pytest.raises(InvalidPortError, match="1..65535"):
            load_port_config()

    def test_empty_string_uses_default(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(ENV_PORT_DASHBOARD, "")
        cfg = load_port_config()
        assert cfg.dashboard == DEFAULT_DASHBOARD_PORT


# ────────────────────────────────────────────────────────────────────────
# detect_environment
# ────────────────────────────────────────────────────────────────────────


class TestDetectEnvironment:
    def test_default_is_terminal(self, clean_env: pytest.MonkeyPatch) -> None:
        # /.dockerenv must not exist (we assume tests run on a normal host)
        assert detect_environment() in (
            RuntimeEnvironment.TERMINAL,
            RuntimeEnvironment.DOCKER,
        )

    def test_claude_cowork_from_remote_env_type(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", "sandbox")
        assert detect_environment() == RuntimeEnvironment.CLAUDE_COWORK

    def test_claude_cowork_from_claudecode_var(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("CLAUDECODE", "1")
        assert detect_environment() == RuntimeEnvironment.CLAUDE_COWORK

    def test_intellij_from_pycharm_hosted(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("PYCHARM_HOSTED", "1")
        assert detect_environment() == RuntimeEnvironment.INTELLIJ

    def test_intellij_from_idea_dir(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("IDEA_INITIAL_DIRECTORY", "/tmp/project")
        assert detect_environment() == RuntimeEnvironment.INTELLIJ

    def test_cowork_takes_precedence_over_intellij(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        """Cowork is checked first because it's more specific."""
        clean_env.setenv("CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", "sandbox")
        clean_env.setenv("PYCHARM_HOSTED", "1")
        assert detect_environment() == RuntimeEnvironment.CLAUDE_COWORK


# ────────────────────────────────────────────────────────────────────────
# detect_lan_ip — best-effort, no network assertion
# ────────────────────────────────────────────────────────────────────────


class TestDetectLanIp:
    def test_returns_string_or_none(self) -> None:
        """In a sandboxed env, this may return None; on a normal Mac, a string."""
        result = detect_lan_ip(timeout=0.1)
        assert result is None or isinstance(result, str)

    def test_none_on_oserror(self) -> None:
        """Simulate network failure → None (not a crash)."""
        with patch("brutex.config.ports.socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.connect.side_effect = (
                OSError("fake network error")
            )
            assert detect_lan_ip() is None


# ────────────────────────────────────────────────────────────────────────
# PortConfig methods
# ────────────────────────────────────────────────────────────────────────


class TestPortConfigUrls:
    def test_dashboard_urls_includes_localhost(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.TERMINAL,
        )
        urls = cfg.dashboard_urls(lan_ip=None)
        assert "http://localhost:9080/" in urls

    def test_dashboard_urls_with_lan_ip(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.TERMINAL,
        )
        urls = cfg.dashboard_urls(lan_ip="192.168.1.42")
        assert "http://192.168.1.42:9080/" in urls
        assert "http://localhost:9080/" in urls
        # LAN IP comes first
        assert urls[0] == "http://192.168.1.42:9080/"

    def test_dashboard_urls_localhost_only_when_bound_127(self) -> None:
        cfg = PortConfig(
            host_bind="127.0.0.1",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.TERMINAL,
        )
        urls = cfg.dashboard_urls(lan_ip="192.168.1.42")
        # Bound to loopback → no LAN URL even if LAN IP is available
        assert urls == ["http://localhost:9080/"]

    def test_metrics_url(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.TERMINAL,
        )
        assert cfg.metrics_url() == "http://localhost:9081/metrics"


class TestPortConfigNotes:
    def test_terminal_has_no_notes(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.TERMINAL,
        )
        assert cfg.environment_notes() == []

    def test_cowork_has_note(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.CLAUDE_COWORK,
        )
        notes = cfg.environment_notes()
        assert len(notes) == 1
        assert "Cowork" in notes[0]

    def test_intellij_has_note(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.INTELLIJ,
        )
        notes = cfg.environment_notes()
        assert len(notes) == 1
        assert "IntelliJ" in notes[0]

    def test_docker_has_note(self) -> None:
        cfg = PortConfig(
            host_bind="0.0.0.0",
            dashboard=9080,
            metrics=9081,
            jupyter=9888,
            ray_dashboard=9265,
            environment=RuntimeEnvironment.DOCKER,
        )
        notes = cfg.environment_notes()
        assert len(notes) == 1
        assert "Docker" in notes[0]


# ────────────────────────────────────────────────────────────────────────
# check_port_free / assert_port_available
# ────────────────────────────────────────────────────────────────────────


class TestPortBinding:
    def test_free_port_returns_true(self) -> None:
        """A high random port is very likely free."""
        assert check_port_free(49152) is True  # dynamic port range

    def test_already_bound_port_returns_false(self) -> None:
        """Bind a port ourselves, then assert check_port_free returns False."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        bound_port = s.getsockname()[1]
        s.listen(1)
        try:
            assert check_port_free(bound_port, host="127.0.0.1") is False
        finally:
            s.close()

    def test_assert_port_available_raises_when_bound(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        bound_port = s.getsockname()[1]
        s.listen(1)
        try:
            with pytest.raises(PortInUseError, match="already in use"):
                assert_port_available(
                    port=bound_port, service="dashboard", host="127.0.0.1"
                )
        finally:
            s.close()


# ────────────────────────────────────────────────────────────────────────
# PortInUseError message formatting
# ────────────────────────────────────────────────────────────────────────


class TestPortInUseErrorMessage:
    def test_includes_lsof_hint(self) -> None:
        err = PortInUseError(port=9080, service="dashboard", host="0.0.0.0")
        assert "lsof -i :9080" in str(err)

    def test_includes_env_var_hint_for_dashboard(self) -> None:
        err = PortInUseError(port=9080, service="dashboard", host="0.0.0.0")
        assert "BRUTEX_PORT_DASHBOARD" in str(err)

    def test_includes_env_var_hint_for_metrics(self) -> None:
        err = PortInUseError(port=9081, service="metrics", host="0.0.0.0")
        assert "BRUTEX_PORT_METRICS" in str(err)

    def test_includes_kill_hint(self) -> None:
        err = PortInUseError(port=9080, service="dashboard", host="0.0.0.0")
        assert "kill" in str(err)

    def test_tickvault_port_has_extra_hint(self) -> None:
        """If you try to bind a known tickvault port, the error mentions tickvault."""
        err = PortInUseError(port=9090, service="metrics", host="0.0.0.0")
        assert "tickvault" in str(err).lower()

    def test_non_tickvault_port_has_no_tickvault_hint(self) -> None:
        err = PortInUseError(port=9080, service="dashboard", host="0.0.0.0")
        assert "tickvault" not in str(err).lower()

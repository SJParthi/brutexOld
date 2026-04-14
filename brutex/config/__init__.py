"""BruteX configuration — ports, URLs, runtime discovery.

This package implements ``NFR-PORT-004``, ``FR-UI-007``, and ``FR-UI-008``
from ``docs/REQUIREMENTS.md``. It's the single source of truth for:

* Which TCP ports BruteX binds (9xxx range, configurable via env vars)
* How to detect the current runtime environment (terminal / IntelliJ /
  Claude Cowork / Docker)
* How to derive user-facing URLs in each environment
* How to write and read the ``~/.brutex/runtime.json`` service discovery
  file so ``brutex status`` can find a running instance

Everything here is **pure, synchronous, offline**. No HTTP server
binding, no async. The FastAPI app in ``brutex.dashboard`` (later
commit) imports :class:`PortConfig` at startup and reads the resolved
values; this package never starts a server itself.
"""

from __future__ import annotations

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
    PortConfig,
    PortInUseError,
    RuntimeEnvironment,
    assert_port_available,
    check_port_free,
    detect_environment,
    detect_lan_ip,
    load_port_config,
)
from brutex.config.runtime import (
    ENV_RUNTIME_FILE,
    DEFAULT_RUNTIME_FILE,
    RuntimeInfo,
    RuntimeFileError,
    clear_stale_runtime,
    is_pid_alive,
    read_runtime_file,
    runtime_file_path,
    write_runtime_file,
)

__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "DEFAULT_HOST_BIND",
    "DEFAULT_JUPYTER_PORT",
    "DEFAULT_METRICS_PORT",
    "DEFAULT_RAY_DASHBOARD_PORT",
    "DEFAULT_RUNTIME_FILE",
    "ENV_HOST_BIND",
    "ENV_PORT_DASHBOARD",
    "ENV_PORT_JUPYTER",
    "ENV_PORT_METRICS",
    "ENV_PORT_RAY",
    "ENV_RUNTIME_FILE",
    "PortConfig",
    "PortInUseError",
    "RuntimeEnvironment",
    "RuntimeFileError",
    "RuntimeInfo",
    "assert_port_available",
    "check_port_free",
    "clear_stale_runtime",
    "detect_environment",
    "detect_lan_ip",
    "is_pid_alive",
    "load_port_config",
    "read_runtime_file",
    "runtime_file_path",
    "write_runtime_file",
]

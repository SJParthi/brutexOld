"""Port and runtime-environment configuration for BruteX.

Implements ``NFR-PORT-004`` and ``FR-UI-008`` from ``docs/REQUIREMENTS.md``.

**Why 9xxx and not 8xxx?**
Tickvault (the author's separate Rust-based live trading system at
``https://github.com/SJParthi/tickvault``) already binds a number of
ports in the 3xxx/6xxx/8xxx/9xxx range via its Docker Compose stack:

* tv-traefik: 80, 443, 8080 (API gateway)
* tv-grafana: 3000
* tv-api: 3001 (Axum HTTP server)
* tv-loki: 3100
* tv-valkey: 6379
* tv-questdb: 8812, 9000, 9009
* tv-prometheus: **9090**  ← WOULD COLLIDE with a naive default
* tv-jaeger: 16686

BruteX chooses ports in the 9xxx range that are **not** used by tickvault
so both products can run side-by-side on the author's MacBook during
development. The collision with tv-prometheus on 9090 was caught during
cross-verification — BruteX's metrics port is therefore **9081**, not 9090.

See ``NFR-PORT-004`` in the requirements doc for the authoritative list.
"""

from __future__ import annotations

import errno
import os
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Defaults — non-negotiable unless overridden via env vars or CLI flags
# ────────────────────────────────────────────────────────────────────────

#: Default dashboard + API + WebSocket port. Serves HTML, /api/v1/*, and /ws.
DEFAULT_DASHBOARD_PORT: Final[int] = 9080

#: Default Prometheus metrics scrape port. **9081, not 9090** — 9090 is
#: reserved for tv-prometheus per tickvault CLAUDE.md.
DEFAULT_METRICS_PORT: Final[int] = 9081

#: Default Jupyter kernel port (only bound when `brutex jupyter` is run).
DEFAULT_JUPYTER_PORT: Final[int] = 9888

#: Default Ray cluster dashboard port (bound in Phase P4+ during
#: brute-force sweeps, not during ingest).
DEFAULT_RAY_DASHBOARD_PORT: Final[int] = 9265

#: Default bind host. ``0.0.0.0`` means "all interfaces" — the operator
#: can reach the dashboard from their phone/tablet on the same LAN.
#: Override to ``127.0.0.1`` for localhost-only.
DEFAULT_HOST_BIND: Final[str] = "0.0.0.0"  # noqa: S104  # nosec B104 — intentional LAN bind

#: Maximum valid TCP/UDP port number (2^16 - 1).
MAX_PORT: Final[int] = 65535

#: Minimum valid TCP/UDP port number — port 0 is reserved.
MIN_PORT: Final[int] = 1


# ────────────────────────────────────────────────────────────────────────
# Env var names — single source of truth
# ────────────────────────────────────────────────────────────────────────

ENV_HOST_BIND: Final[str] = "BRUTEX_HOST_BIND"
ENV_PORT_DASHBOARD: Final[str] = "BRUTEX_PORT_DASHBOARD"
ENV_PORT_METRICS: Final[str] = "BRUTEX_PORT_METRICS"
ENV_PORT_JUPYTER: Final[str] = "BRUTEX_PORT_JUPYTER"
ENV_PORT_RAY: Final[str] = "BRUTEX_PORT_RAY"


# ────────────────────────────────────────────────────────────────────────
# Tickvault-reserved ports — NEVER bind these
# ────────────────────────────────────────────────────────────────────────

#: Ports owned by tickvault's Docker Compose stack. BruteX must not
#: claim these as defaults. Cross-verified on 2026-04-14 from
#: https://github.com/SJParthi/tickvault/blob/main/CLAUDE.md#docker-services.
TICKVAULT_RESERVED_PORTS: Final[frozenset[int]] = frozenset(
    {
        80,  # tv-traefik (HTTP)
        443,  # tv-traefik (HTTPS)
        3000,  # tv-grafana
        3001,  # tv-api (Axum)
        3100,  # tv-loki
        6379,  # tv-valkey (Redis)
        8080,  # tv-traefik (dashboard)
        8812,  # tv-questdb (PG-wire)
        9000,  # tv-questdb (HTTP)
        9009,  # tv-questdb (ILP)
        9090,  # tv-prometheus  ← the one we collided with
        16686,  # tv-jaeger
    }
)


def is_tickvault_reserved_port(port: int) -> bool:
    """Return True if *port* is documented to be used by tickvault."""
    return port in TICKVAULT_RESERVED_PORTS


# ────────────────────────────────────────────────────────────────────────
# Runtime environment detection (FR-UI-008)
# ────────────────────────────────────────────────────────────────────────


class RuntimeEnvironment(StrEnum):
    """Where BruteX is currently running.

    Detected at startup to generate the correct user-facing URLs
    in the dashboard banner and the ``brutex status`` output.

    * :attr:`TERMINAL` — plain Mac/Linux terminal (default)
    * :attr:`CLAUDE_COWORK` — Claude Code web sandbox (container with
      forwarded ports); detected via ``CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE``
    * :attr:`INTELLIJ` — IntelliJ / PyCharm run configuration; detected
      via ``PYCHARM_HOSTED`` or ``IDEA_INITIAL_DIRECTORY``
    * :attr:`DOCKER` — generic Docker container; detected via
      ``/.dockerenv``
    * :attr:`UNKNOWN` — nothing matched
    """

    TERMINAL = "terminal"
    CLAUDE_COWORK = "cowork"
    INTELLIJ = "intellij"
    DOCKER = "docker"
    UNKNOWN = "unknown"


def detect_environment() -> RuntimeEnvironment:
    """Detect the current runtime environment.

    Pure function, reads only ``os.environ`` and ``/.dockerenv``.
    Ordering matters: more-specific environments are checked first
    because Cowork runs inside a Docker container (would otherwise
    match DOCKER).
    """
    # Claude Cowork sets several env vars that no other environment uses.
    if os.environ.get("CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE"):
        return RuntimeEnvironment.CLAUDE_COWORK
    if os.environ.get("CLAUDECODE"):
        return RuntimeEnvironment.CLAUDE_COWORK

    # IntelliJ / PyCharm.
    if os.environ.get("PYCHARM_HOSTED") == "1":
        return RuntimeEnvironment.INTELLIJ
    if os.environ.get("IDEA_INITIAL_DIRECTORY"):
        return RuntimeEnvironment.INTELLIJ

    # Generic Docker (must be checked AFTER Cowork, since Cowork is
    # also inside a container).
    if os.path.exists("/.dockerenv"):
        return RuntimeEnvironment.DOCKER

    # Plain terminal / run from shell.
    return RuntimeEnvironment.TERMINAL


# ────────────────────────────────────────────────────────────────────────
# LAN IP detection for phone/tablet URL
# ────────────────────────────────────────────────────────────────────────


def detect_lan_ip(*, timeout: float = 0.5) -> str | None:
    """Detect the machine's LAN IP address.

    Uses the "UDP socket to public IP" trick: creating an unconnected
    UDP socket and calling ``getsockname()`` after a ``connect`` to
    a public address returns the kernel-selected source interface,
    without sending any actual packets. A short timeout prevents
    hanging in offline environments.

    Returns
    -------
    str | None
        The LAN IP as a string (e.g., ``"192.168.1.42"``) or ``None``
        if detection fails (offline, sandboxed, or no network at all).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            # Any routable address works — we don't actually send anything.
            # Google DNS is a common, stable pick.
            s.connect(("8.8.8.8", 80))
            ip: str = s.getsockname()[0]
    except OSError:
        return None
    else:
        # Loopback means we had no real interface — treat as "no LAN".
        if ip.startswith("127."):
            return None
        return ip


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class PortInUseError(BruteXError):
    """Raised when BruteX tries to bind a port that's already taken.

    The error message includes diagnostic hints: the offending port,
    the env var to override it, an ``lsof`` command to find the
    conflicting process, and — if the port matches a known tickvault
    service — an explicit hint that tickvault may be running.
    """

    def __init__(self, *, port: int, service: str, host: str) -> None:
        self.port = port
        self.service = service
        self.host = host

        env_var_map = {
            "dashboard": ENV_PORT_DASHBOARD,
            "metrics": ENV_PORT_METRICS,
            "jupyter": ENV_PORT_JUPYTER,
            "ray": ENV_PORT_RAY,
        }
        env_var = env_var_map.get(service, "BRUTEX_PORT_*")

        lines = [
            f"Port {port} ({service}) is already in use on {host}.",
            f"  → Check what's using it: lsof -i :{port}",
            f"  → Override the port:    {env_var}=<different_port> brutex ...",
            f"  → Kill the other proc:  kill $(lsof -t -i :{port})",
        ]
        if is_tickvault_reserved_port(port):
            lines.append(
                f"  ⚠️  Port {port} is documented as a tickvault service "
                f"(see tickvault CLAUDE.md §Docker Services). Check if "
                f"tickvault's Docker stack is running: `make docker-ps` "
                f"inside the tickvault repo."
            )
        super().__init__("\n".join(lines))


class InvalidPortError(BruteXError):
    """Raised when a port value is malformed or out of range."""


# ────────────────────────────────────────────────────────────────────────
# PortConfig — the resolved, frozen configuration
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PortConfig:
    """Resolved port configuration for one BruteX process.

    Built via :func:`load_port_config` from env vars (with hard-coded
    defaults when unset). Immutable — any change requires a new
    ``PortConfig`` instance, so tests and callers can rely on identity.
    """

    host_bind: str
    dashboard: int
    metrics: int
    jupyter: int
    ray_dashboard: int
    environment: RuntimeEnvironment

    def dashboard_urls(self, lan_ip: str | None = None) -> list[str]:
        """Return ordered user-facing URLs for the dashboard.

        The first URL in the list is the "primary" one — what the
        banner should print most prominently. The rest are alternates.

        Parameters
        ----------
        lan_ip
            Override the LAN IP (used by tests). If ``None``, calls
            :func:`detect_lan_ip` to detect it.

        Returns
        -------
        list[str]
            One or more URLs, never empty. Always includes the
            localhost form as a fallback.
        """
        urls: list[str] = []

        # LAN IP goes first when host_bind is 0.0.0.0 and we have an IP.
        if self.host_bind in ("0.0.0.0", "::"):  # noqa: S104  # nosec B104 — intentional LAN compare
            ip = lan_ip if lan_ip is not None else detect_lan_ip()
            if ip:
                urls.append(f"http://{ip}:{self.dashboard}/")

        # localhost always works regardless of bind.
        urls.append(f"http://localhost:{self.dashboard}/")

        return urls

    def metrics_url(self) -> str:
        """Return the Prometheus scrape URL."""
        return f"http://localhost:{self.metrics}/metrics"

    def environment_notes(self) -> list[str]:
        """Return human-readable notes about the current environment.

        Used by the dashboard startup banner. One line per note.
        """
        notes: list[str] = []
        if self.environment == RuntimeEnvironment.CLAUDE_COWORK:
            notes.append(
                "Running inside Claude Cowork — dashboard may be "
                "auto-forwarded. Check the Cowork session panel for "
                "the forwarded URL."
            )
        elif self.environment == RuntimeEnvironment.INTELLIJ:
            notes.append(
                "Running inside IntelliJ / PyCharm — check the "
                "Services panel for port forwarding status."
            )
        elif self.environment == RuntimeEnvironment.DOCKER:
            notes.append(
                "Running inside a Docker container — make sure the "
                "host-side port mapping (e.g., `-p 9080:9080`) is set "
                "or the dashboard will not be reachable from the host."
            )
        return notes


# ────────────────────────────────────────────────────────────────────────
# Loader
# ────────────────────────────────────────────────────────────────────────


def _parse_port(env_var: str, default: int) -> int:
    """Parse and validate a port number from an environment variable string."""
    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise InvalidPortError(
            f"Invalid {env_var}={raw!r} — must be an integer in {MIN_PORT}..{MAX_PORT}"
        ) from exc
    if not (MIN_PORT <= port <= MAX_PORT):
        raise InvalidPortError(f"Invalid {env_var}={port} — must be in {MIN_PORT}..{MAX_PORT}")
    return port


def load_port_config() -> PortConfig:
    """Read env vars + defaults to build a :class:`PortConfig`.

    Env var precedence (highest wins):

    1. Explicit env var set (``BRUTEX_PORT_DASHBOARD=9085``)
    2. Hard-coded default (``DEFAULT_DASHBOARD_PORT = 9080``)

    Environment detection always runs.

    Raises
    ------
    InvalidPortError
        If any env var is non-numeric or outside 1..65535.
    """
    return PortConfig(
        host_bind=os.environ.get(ENV_HOST_BIND, DEFAULT_HOST_BIND),
        dashboard=_parse_port(ENV_PORT_DASHBOARD, DEFAULT_DASHBOARD_PORT),
        metrics=_parse_port(ENV_PORT_METRICS, DEFAULT_METRICS_PORT),
        jupyter=_parse_port(ENV_PORT_JUPYTER, DEFAULT_JUPYTER_PORT),
        ray_dashboard=_parse_port(ENV_PORT_RAY, DEFAULT_RAY_DASHBOARD_PORT),
        environment=detect_environment(),
    )


# ────────────────────────────────────────────────────────────────────────
# Port availability probe
# ────────────────────────────────────────────────────────────────────────


def check_port_free(port: int, host: str = DEFAULT_HOST_BIND) -> bool:
    """Check whether *port* can be bound on *host* right now.

    Non-destructive: opens a socket, tries to bind, releases immediately.
    Returns ``True`` if the bind succeeded, ``False`` otherwise (bind
    error, EADDRINUSE, permission denied, etc.).

    Note: this is an **observation at a point in time**. Between the
    check and an actual bind there's a TOCTOU race — use
    :func:`assert_port_available` as an early-warning, but the real
    authority is the FastAPI/uvicorn startup which will raise its own
    error if it can't bind. Both are valid.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            # Permission denied (common for ports < 1024) or other
            # bind errors are treated as "not free" — the caller
            # should see the error either way.
            return False
        else:
            return True


def assert_port_available(
    *,
    port: int,
    service: str,
    host: str = DEFAULT_HOST_BIND,
) -> None:
    """Raise :exc:`PortInUseError` if *port* is not bindable.

    Called by the dashboard / metrics / jupyter launcher before the
    actual server starts, so we can emit a helpful error message
    with diagnostic hints instead of a raw OSError from uvicorn.

    Parameters
    ----------
    port
        Port to probe.
    service
        Short service name for the error message — one of ``dashboard``,
        ``metrics``, ``jupyter``, ``ray`` (others get the generic
        ``BRUTEX_PORT_*`` hint).
    host
        Interface to test against. Defaults to ``DEFAULT_HOST_BIND``.
    """
    if not check_port_free(port, host):
        raise PortInUseError(port=port, service=service, host=host)


__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "DEFAULT_HOST_BIND",
    "DEFAULT_JUPYTER_PORT",
    "DEFAULT_METRICS_PORT",
    "DEFAULT_RAY_DASHBOARD_PORT",
    "ENV_HOST_BIND",
    "ENV_PORT_DASHBOARD",
    "ENV_PORT_JUPYTER",
    "ENV_PORT_METRICS",
    "ENV_PORT_RAY",
    "TICKVAULT_RESERVED_PORTS",
    "InvalidPortError",
    "PortConfig",
    "PortInUseError",
    "RuntimeEnvironment",
    "assert_port_available",
    "check_port_free",
    "detect_environment",
    "detect_lan_ip",
    "is_tickvault_reserved_port",
    "load_port_config",
]

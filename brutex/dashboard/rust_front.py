"""Default-OFF Rust front (``brutexd``) launcher — Rust-uniformity Wave 1.

The ONE Python seam of the SERVING track's opener PR (roadmap_final.md §3):
when — and ONLY when — the fail-loud env flag ``BRUTEX_RUST_FRONT=on`` is
set, ``brutex dashboard`` binds uvicorn to a PRIVATE loopback port and
spawns the ``brutexd`` binary on the OPERATOR port. brutexd serves
``/static/*`` + ``/health`` natively and reverse-proxies every other path
verbatim to the FastAPI job host (the parity contract is gated by
``tests/integration/test_sidecar_parity_static.py``).

Flag law (house env-law — the ``BRUTEX_LOG_LEVEL`` pattern):

* absent / empty / ``off`` → **OFF** (the default): no brutexd process is
  spawned, ``cmd_dashboard`` runs uvicorn on the operator port exactly as
  before this module existed — byte-zero behaviour change, pinned by
  ``tests/unit/test_rust_front.py``;
* ``on`` → the two-process front topology above;
* anything else → :class:`RustFrontError`, mapped to ``CliUserError`` exit 1
  at the CLI boundary — never a silent fallback.

Rollback = unset the flag (one env var, no deploy); the Python static mount
survives this PR untouched (``docs/runbooks/rust-front.md``).

HONEST (Golden Rule #10): this is a **uniformity program, not a performance
program** — no latency win is claimed or measured here. Wave 1 is
loopback-only: a non-loopback ``--host`` under the flag is REFUSED loudly
(the dashboard has no auth; widening the Rust front's bind is out of scope).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess  # nosec B404 — fixed brutexd argv built below, no shell, no user input
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

_LOG = structlog.get_logger("brutex.dashboard.rust_front")

#: The fail-loud enable flag (house env-law: junk → loud refusal, exit 1).
ENV_RUST_FRONT = "BRUTEX_RUST_FRONT"

#: Optional explicit path to the brutexd binary (highest-priority lookup).
ENV_BRUTEXD_BIN = "BRUTEX_BRUTEXD_BIN"

#: Host spellings accepted for the Wave-1 loopback-only front bind.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: How long to wait for brutexd to exit after terminate()/kill() (seconds).
TERMINATE_TIMEOUT_S = 5.0


class RustFrontError(Exception):
    """Loud Rust-front configuration/launch refusal.

    Raised for a junk ``BRUTEX_RUST_FRONT`` value, a missing ``brutexd``
    binary, a non-loopback host under the flag, or a missing static dir.
    ``cmd_dashboard`` maps it to ``CliUserError`` → one ``[brutex] …`` line +
    exit 1 (never a silent fallback, never a half-started topology).
    """


class ProcessHandle(Protocol):
    """The subset of ``subprocess.Popen`` the supervisor needs (test seam)."""

    def poll(self) -> int | None:
        """Return the exit code if the process has exited, else ``None``."""
        ...  # pragma: no cover — Protocol stub

    def terminate(self) -> None:
        """Send SIGTERM."""
        ...  # pragma: no cover — Protocol stub

    def kill(self) -> None:
        """Send SIGKILL."""
        ...  # pragma: no cover — Protocol stub

    def wait(self, timeout: float | None = None) -> int:
        """Wait for exit, raising ``subprocess.TimeoutExpired`` on timeout."""
        ...  # pragma: no cover — Protocol stub


def resolve_rust_front(env: Mapping[str, str] | None = None) -> bool:
    """Read + validate ``BRUTEX_RUST_FRONT`` (fail-loud on junk).

    Parameters
    ----------
    env
        Environment mapping (defaults to ``os.environ``; injection seam).

    Returns
    -------
    bool
        ``True`` iff the flag is exactly ``on`` (case-insensitive, stripped).

    Raises
    ------
    RustFrontError
        For any value outside ``{"", "on", "off"}`` — the house env-law:
        a typo must refuse the start loudly, never silently run the
        default topology.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_RUST_FRONT)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in ("", "off"):
        return False
    if normalized == "on":
        return True
    raise RustFrontError(
        f"invalid {ENV_RUST_FRONT}={raw!r} — must be 'on' or 'off' (default off); "
        "refusing to start rather than guessing the topology"
    )


def default_static_dir() -> Path:
    """Return the packaged ``brutex/dashboard/static/`` directory."""
    return Path(__file__).resolve().parent / "static"


def _default_repo_root() -> Path:
    """Best-guess repo root (``brutex/dashboard/rust_front.py`` → two up)."""
    return Path(__file__).resolve().parents[2]


def locate_brutexd(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Locate the ``brutexd`` binary (fail-loud when the flag is on).

    Lookup order: the explicit ``BRUTEX_BRUTEXD_BIN`` override (must exist —
    a dangling override is a refusal, never a fallthrough) → the in-repo
    cargo target dirs (``rust/brutexd/target/{release,debug}/brutexd``) →
    ``PATH`` via :func:`shutil.which`.

    Raises
    ------
    RustFrontError
        When no binary is found — the message names the build command and
        the override env var (remediation-naming refusal).
    """
    source = os.environ if env is None else env
    override = source.get(ENV_BRUTEXD_BIN)
    if override is not None and override.strip():
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
        raise RustFrontError(
            f"{ENV_BRUTEXD_BIN}={override!r} does not point at a file — fix or unset the override"
        )
    root = _default_repo_root() if repo_root is None else repo_root
    for profile in ("release", "debug"):
        candidate = root / "rust" / "brutexd" / "target" / profile / "brutexd"
        if candidate.is_file():
            return candidate
    which = shutil.which("brutexd")
    if which is not None:
        return Path(which)
    raise RustFrontError(
        "brutexd binary not found — build it with "
        "`cargo build --release --manifest-path rust/brutexd/Cargo.toml` "
        f"or point {ENV_BRUTEXD_BIN} at an existing binary"
    )


def allocate_upstream_port() -> int:
    """Allocate a free loopback port for the private uvicorn bind.

    Bind-to-port-0 then release: the kernel hands back a currently-free
    ephemeral port. HONEST: there is a documented TOCTOU window between the
    release here and uvicorn's own bind — if another process grabs the port
    in between, uvicorn fails its bind LOUDLY (the same failure mode a fixed
    port already has today), never silently.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True, slots=True)
class RustFrontPlan:
    """The resolved two-process topology for one ``brutex dashboard`` run."""

    binary: Path
    listen_host: str
    listen_port: int
    upstream_port: int
    static_dir: Path


def build_plan(
    *,
    host: str,
    port: int,
    env: Mapping[str, str] | None = None,
    static_dir: Path | None = None,
    upstream_port: int | None = None,
) -> RustFrontPlan:
    """Resolve + validate the front topology (every violation fail-loud).

    Parameters
    ----------
    host, port
        The OPERATOR bind requested on the CLI — brutexd takes it over.
    env
        Environment mapping (binary-override lookup; injection seam).
    static_dir
        Override for tests; defaults to the packaged static tree.
    upstream_port
        Override for tests; defaults to a freshly allocated free port.

    Raises
    ------
    RustFrontError
        Non-loopback host (Wave-1 loopback-only law), missing static dir,
        or no brutexd binary.
    """
    if host not in LOOPBACK_HOSTS:
        raise RustFrontError(
            f"refusing {ENV_RUST_FRONT}=on with non-loopback --host {host!r} — "
            "the Rust front is 127.0.0.1-only in Wave 1 (the dashboard has no "
            "auth; unset the flag to serve beyond loopback)"
        )
    resolved_static = default_static_dir() if static_dir is None else static_dir
    if not resolved_static.is_dir():
        raise RustFrontError(f"static dir {str(resolved_static)!r} is not a directory")
    binary = locate_brutexd(env)
    resolved_upstream = allocate_upstream_port() if upstream_port is None else upstream_port
    return RustFrontPlan(
        binary=binary,
        listen_host=host,
        listen_port=port,
        upstream_port=resolved_upstream,
        static_dir=resolved_static,
    )


def build_command(plan: RustFrontPlan) -> list[str]:
    """Render the fixed brutexd argv for *plan* (no shell, no user input)."""
    return [
        str(plan.binary),
        "--listen",
        f"{plan.listen_host}:{plan.listen_port}",
        "--upstream-port",
        str(plan.upstream_port),
        "--static-dir",
        str(plan.static_dir),
    ]


def _shutdown(proc: ProcessHandle) -> None:
    """Terminate the supervised brutexd child (terminate → wait → kill)."""
    if proc.poll() is not None:
        _LOG.warning("rust_front.brutexd_exited_early", returncode=proc.poll())
        return
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _LOG.warning("rust_front.brutexd_kill_escalation")
        proc.kill()
        proc.wait(timeout=TERMINATE_TIMEOUT_S)
    _LOG.info("rust_front.brutexd_stopped")


def run_with_rust_front(
    app: Any,
    *,
    host: str,
    port: int,
    uvicorn_run: Callable[..., Any],
    env: Mapping[str, str] | None = None,
    popen: Callable[[list[str]], ProcessHandle] = subprocess.Popen,
) -> None:
    """Run the two-process front topology for one dashboard session.

    Spawns brutexd on the operator ``host:port`` (proxying to a private
    loopback port), then runs uvicorn — the blocking call — on that private
    port. brutexd is SUPERVISED: whenever uvicorn returns or raises, the
    child is terminated in the ``finally`` (never an orphan by design; a
    SIGKILLed parent is the documented residual — see the runbook).

    During uvicorn's own startup window brutexd answers proxied paths with
    its honest plain-body 502 ("upstream unreachable") — a visible degrade,
    never a hang or a fabricated response.
    """
    plan = build_plan(host=host, port=port, env=env)
    command = build_command(plan)
    _LOG.info(
        "rust_front.starting",
        binary=str(plan.binary),
        listen=f"{plan.listen_host}:{plan.listen_port}",
        upstream_port=plan.upstream_port,
    )
    proc = popen(command)  # nosec B603 — fixed argv from build_command, no shell
    try:
        uvicorn_run(app, host="127.0.0.1", port=plan.upstream_port, log_config=None)
    finally:
        _shutdown(proc)


__all__ = [
    "ENV_BRUTEXD_BIN",
    "ENV_RUST_FRONT",
    "LOOPBACK_HOSTS",
    "TERMINATE_TIMEOUT_S",
    "ProcessHandle",
    "RustFrontError",
    "RustFrontPlan",
    "allocate_upstream_port",
    "build_command",
    "build_plan",
    "default_static_dir",
    "locate_brutexd",
    "resolve_rust_front",
    "run_with_rust_front",
]

"""Default-AUTO-ON Rust front (``brutexd``) launcher — Rust-uniformity Wave 1.

The ONE Python seam of the SERVING track's opener PR (roadmap_final.md §3):
under the ``BRUTEX_RUST_FRONT`` flag, ``brutex dashboard`` binds uvicorn to
a PRIVATE loopback port and spawns the ``brutexd`` binary on the OPERATOR
port. brutexd serves ``/static/*`` + ``/health`` natively and
reverse-proxies every other path verbatim to the FastAPI job host (the
parity contract is gated by ``tests/integration/test_sidecar_parity_static.py``).

Flag law (house env-law — the ``BRUTEX_LOG_LEVEL`` pattern; default
**AUTO-ON** per DEC-RUST-DEFAULT-ON-WAVE-001, operator directive
2026-07-23 — supersedes the default-OFF arm of
DEC-RUST-FRONT-STATIC-HEALTH-001):

* absent → **AUTO** (the default): the front preconditions are probed
  (:func:`probe_auto_front` — the :func:`locate_brutexd` ladder, the
  loopback-host law, the static dir). ALL hold → the two-process front
  topology, exactly as explicit ``on``; ANY fails → ONE loud WARNING
  naming the missing precondition + the cargo build remediation, then the
  exact legacy Python-only topology — NEVER a fatal boot on the AUTO path;
* empty / ``off`` → **OFF**: no brutexd process is spawned,
  ``cmd_dashboard`` runs uvicorn on the operator port exactly as before
  this module existed — pinned by ``tests/unit/test_rust_front.py``;
* ``on`` → the two-process front topology, STRICT: a missing binary /
  non-loopback host / missing static dir is the same loud
  :class:`RustFrontError` fatal refusal as ever — an operator who pins the
  flag ON must be told loudly, never silently degraded;
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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

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

#: How long to let brutexd prove it stayed up before entering uvicorn (seconds).
#: A bounded ONE-TIME startup grace — not a poll loop; see ``_default_liveness_probe``.
STARTUP_GRACE_S = 0.5

#: Loopback ``--listen`` host rendering: the operator host spelling → a token
#: that ALWAYS parses as a Rust ``SocketAddr`` when joined with ``:<port>``.
#: ``rust/brutexd/src/config.rs`` parses ``--listen`` as a numeric ``SocketAddr``,
#: so ``localhost`` (not an IP) and a bare ``::1`` (needs the RFC 3986 bracket
#: form ``[::1]`` before the port) would be REJECTED verbatim — brutexd would
#: exit 2 and the operator port would go dead. Every other host passes through.
_LISTEN_HOST_RENDER = {"localhost": "127.0.0.1", "::1": "[::1]"}


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

    def terminate(self) -> None:
        """Send SIGTERM."""

    def kill(self) -> None:
        """Send SIGKILL."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for exit, raising ``subprocess.TimeoutExpired`` on timeout."""
        raise NotImplementedError  # pragma: no cover — Protocol stub


#: The three resolved flag states (DEC-RUST-DEFAULT-ON-WAVE-001):
#: ``"auto"`` (the UNSET default — probe-then-decide), ``"on"`` (strict,
#: fail-loud) and ``"off"`` (the legacy Python-only topology).
RustFrontMode = Literal["auto", "on", "off"]


def resolve_rust_front_mode(env: Mapping[str, str] | None = None) -> RustFrontMode:
    """Read + validate ``BRUTEX_RUST_FRONT`` into its tri-state mode.

    Parameters
    ----------
    env
        Environment mapping (defaults to ``os.environ``; injection seam).

    Returns
    -------
    RustFrontMode
        ``"auto"`` when the flag is ABSENT (the DEC-RUST-DEFAULT-ON-WAVE-001
        default — the caller probes :func:`probe_auto_front` and falls back
        gracefully); ``"on"`` for exactly ``on`` (strict fail-loud);
        ``"off"`` for an EXPLICIT empty/``off`` value (case-insensitive,
        stripped — a set-but-blank flag is an explicit off, never AUTO).

    Raises
    ------
    RustFrontError
        For any value outside ``{"", "on", "off"}`` — the house env-law:
        a typo must refuse the start loudly, never silently run some
        guessed topology (unchanged from the default-OFF era).
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_RUST_FRONT)
    if raw is None:
        return "auto"
    normalized = raw.strip().lower()
    if normalized in ("", "off"):
        return "off"
    if normalized == "on":
        return "on"
    raise RustFrontError(
        f"invalid {ENV_RUST_FRONT}={raw!r} — must be 'on' or 'off' "
        "(default auto: on when brutexd is available); "
        "refusing to start rather than guessing the topology"
    )


def resolve_rust_front(env: Mapping[str, str] | None = None) -> bool:
    """Report whether ``BRUTEX_RUST_FRONT`` is EXPLICITLY ``on`` (fail-loud on junk).

    The strict-arm view of :func:`resolve_rust_front_mode` — kept for
    callers that only care about the explicit pin. The UNSET default is no
    longer plain OFF: it is AUTO (DEC-RUST-DEFAULT-ON-WAVE-001), resolved by
    the caller via :func:`resolve_rust_front_mode` + :func:`probe_auto_front`;
    this helper still returns ``False`` for unset (not explicitly on).

    Raises
    ------
    RustFrontError
        For any value outside ``{"", "on", "off"}`` — the house env-law.
    """
    return resolve_rust_front_mode(env) == "on"


def probe_auto_front(
    *,
    host: str,
    port: int,
    env: Mapping[str, str] | None = None,
    static_dir: Path | None = None,
) -> bool:
    """The AUTO arm (DEC-RUST-DEFAULT-ON-WAVE-001): probe the front preconditions.

    Called when ``BRUTEX_RUST_FRONT`` is UNSET. Runs the exact
    :func:`build_plan` validation — the :func:`locate_brutexd` ladder, the
    Wave-1 loopback-host law, the static-dir presence — WITHOUT spawning
    anything (a throwaway ``upstream_port=0`` sentinel keeps the probe from
    allocating a real port; the launcher re-plans for real).

    Returns ``True`` when every precondition holds (the caller then proceeds
    exactly as explicit ``on``). On ANY :class:`RustFrontError` it emits ONE
    loud WARNING naming the missing precondition + the remediation and
    returns ``False`` — the caller falls back to the exact legacy
    Python-only topology. This probe never raises for a plan-precondition
    failure (junk env values are refused earlier by
    :func:`resolve_rust_front_mode`, never here); a genuine code bug still
    propagates (no blanket except).
    """
    try:
        build_plan(host=host, port=port, env=env, static_dir=static_dir, upstream_port=0)
    except RustFrontError as exc:
        _LOG.warning(
            "rust_front.auto_fallback_python",
            reason=str(exc),
            remediation=(
                "serving via the legacy Python-only topology; to enable the "
                "Rust front build brutexd with `cargo build --release "
                "--manifest-path rust/brutexd/Cargo.toml` (or point "
                f"{ENV_BRUTEXD_BIN} at a binary); set {ENV_RUST_FRONT}=off "
                "to silence this warning"
            ),
        )
        return False
    return True


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


def render_listen(host: str, port: int) -> str:
    """Render the brutexd ``--listen`` value so it ALWAYS parses as a SocketAddr.

    The operator-facing host is preserved everywhere else (``RustFrontPlan``
    keeps the requested spelling); ONLY the concrete ``--listen`` argument is
    normalized — ``localhost`` → ``127.0.0.1`` and ``::1`` → ``[::1]`` — so the
    Rust ``SocketAddr`` parse in ``rust/brutexd/src/config.rs`` accepts it
    (a bare ``localhost:9080`` / ``::1:9080`` would be REJECTED, brutexd would
    exit 2, and the operator port would be silently dead). A host already in IP
    form passes through unchanged. The loopback-only law is upheld upstream by
    :func:`build_plan` (a non-loopback host never reaches here), so the render
    never widens the bind.
    """
    return f"{_LISTEN_HOST_RENDER.get(host, host)}:{port}"


def build_command(plan: RustFrontPlan) -> list[str]:
    """Render the fixed brutexd argv for *plan* (no shell, no user input)."""
    return [
        str(plan.binary),
        "--listen",
        render_listen(plan.listen_host, plan.listen_port),
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
        try:
            proc.wait(timeout=TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # A SIGKILLed child that STILL will not reap (a wedged uninterruptible
            # syscall) must not mask uvicorn's own exception on the way out —
            # log the anomaly and return rather than raise from the finally.
            _LOG.warning("rust_front.brutexd_unkillable")
            return
    _LOG.info("rust_front.brutexd_stopped")


def _default_liveness_probe(
    proc: ProcessHandle,
    *,
    grace_s: float = STARTUP_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Give brutexd a bounded moment to fail fast, then report its exit code.

    Returns ``None`` while the child is still alive (the healthy path) or the
    exit code if brutexd already died (bad ``--listen``, operator port already
    in use, static dir gone). ``sleep`` is injectable so tests are deterministic
    without a real wall-clock wait.
    """
    sleep(grace_s)
    return proc.poll()


def run_with_rust_front(
    app: Any,
    *,
    host: str,
    port: int,
    uvicorn_run: Callable[..., Any],
    env: Mapping[str, str] | None = None,
    popen: Callable[[list[str]], ProcessHandle] = subprocess.Popen,
    liveness_probe: Callable[[ProcessHandle], int | None] = _default_liveness_probe,
) -> None:
    """Run the two-process front topology for one dashboard session.

    Spawns brutexd on the operator ``host:port`` (proxying to a private
    loopback port), then runs uvicorn — the blocking call — on that private
    port. brutexd is SUPERVISED: whenever uvicorn returns or raises, the
    child is terminated in the ``finally`` (never an orphan by design; a
    SIGKILLed parent is the documented residual — see the runbook).

    Between the spawn and entering uvicorn, ``liveness_probe`` confirms brutexd
    did not die on the spot (bad ``--listen``, operator port already in use,
    static dir gone). If it already exited, we raise :class:`RustFrontError`
    LOUDLY instead of sailing into uvicorn on the private port — which would
    otherwise leave the operator port DEAD with no error (the half-up
    dashboard). The probe is injectable + bounded so it never introduces a
    flaky wait.

    During uvicorn's own startup window brutexd answers proxied paths with
    its honest plain-body 502 ("upstream unreachable") — a visible degrade,
    never a hang or a fabricated response.
    """
    plan = build_plan(host=host, port=port, env=env)
    command = build_command(plan)
    _LOG.info(
        "rust_front.starting",
        binary=str(plan.binary),
        listen=render_listen(plan.listen_host, plan.listen_port),
        upstream_port=plan.upstream_port,
    )
    proc = popen(command)  # nosec B603 — fixed argv from build_command, no shell
    # The spawn+probe pair is supervised too: if ``liveness_probe`` RAISES
    # (e.g. a KeyboardInterrupt during the startup-grace sleep) OR the child
    # is found already dead, the spawned brutexd is torn down BEFORE the
    # exception propagates — never leaked (the module's "never an orphan by
    # design" law). ``_shutdown`` no-ops safely on an already-dead child
    # (its ``poll()`` guard), so the dead-child path terminates nothing.
    try:
        returncode = liveness_probe(proc)
        if returncode is not None:
            raise RustFrontError(
                f"brutexd exited immediately (code {returncode}) before uvicorn could bind "
                f"the operator port {plan.listen_host}:{plan.listen_port} — port busy? bad "
                "--host? see docs/runbooks/rust-front.md"
            )
    except BaseException:
        _shutdown(proc)
        raise
    try:
        uvicorn_run(app, host="127.0.0.1", port=plan.upstream_port, log_config=None)
    finally:
        _shutdown(proc)


__all__ = [
    "ENV_BRUTEXD_BIN",
    "ENV_RUST_FRONT",
    "LOOPBACK_HOSTS",
    "STARTUP_GRACE_S",
    "TERMINATE_TIMEOUT_S",
    "ProcessHandle",
    "RustFrontError",
    "RustFrontMode",
    "RustFrontPlan",
    "allocate_upstream_port",
    "build_command",
    "build_plan",
    "default_static_dir",
    "locate_brutexd",
    "probe_auto_front",
    "render_listen",
    "resolve_rust_front",
    "resolve_rust_front_mode",
    "run_with_rust_front",
]

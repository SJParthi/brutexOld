"""INTERIM Run-Sweep launch page (DEC-SWEEP-LAUNCH-BUTTON-INTERIM-001).

The one-night launch surface for the operator's full NIFTY sweep: a ``/launch``
page showing tonight's LOCKED sweep configuration (futures-primary +
spot-fallback cards), cheap server-side precondition checks, and a
single-flight, double-click-proof POST that launches ``brutex sweep-all`` as a
**DETACHED subprocess** — so a dashboard restart can never kill or
orphan-track the multi-day run. INTERIM by design: the canonical selector
button ships later from another workstream; this module is strictly additive
and self-contained (one module + one template + one ``build_app`` registration
hunk).

Why a CLI subprocess and not the in-process ``/api/v1/sweep/run`` job
---------------------------------------------------------------------
The existing dashboard sweep route runs ``run_sweep_from_request`` in-process
via :func:`~brutex.dashboard.sweep_runner.build_sweep_kwargs`, which pins the
light-mode dashboard defaults (``allow_missing_slices=True``, guards off) and
dies with the dashboard process. Tonight's locked run must (a) survive a
dashboard restart, (b) carry the exact operator-locked flag set including the
honest ``--max-combos`` guard + ``--acknowledge-sla-overrun``, and (c) refuse
loudly on missing lake slices instead of silently skipping them. Launching the
REAL ``brutex sweep-all`` CLI as a detached process (``start_new_session=True``,
no ``.wait()``, no owning thread) is therefore a DELIBERATE bypass of the
``build_sweep_kwargs`` light-mode pin — recorded in the ledger entry.

Locked command corrections (audit evidence: the prior session's
``phase3/W_1264_branch_validation.md`` branch validation):

* ``--end`` takes a bare date whose session is EXCLUDED on the futures path —
  the locked command therefore passes ``end = today + 1 day``
  (:func:`launch_end_date`) so the final session is never silently dropped.
* The ``1day`` timeframe on futures is structurally TRADE-less — the page's
  grid summary says "20 trade-bearing TFs + 1day (no trades by design)",
  never implying 21 productive timeframes (:data:`GRID_HONESTY_NOTE`).

Futures dependency (PR #1264): the ``NSE-NIFTY-FUT`` whole-series alias
resolves only once PR #1264's alias layer merges. :func:`probe_futures_alias`
probes the REAL scope classifier at request time (guarded import — never a
hardcoded assumption, never a crash when absent); until it resolves, the
futures button is disabled with an honest reason and the spot card stays live.

Single-flight law: an advisory :func:`fcntl.flock` on a stable sidecar under
``<lake_root>/.locks/`` guards the launch critical section, and a pidfile
(``<lake_root>/sweeps/.launch/active_launch.json``) carries the active run.
A second click while the run is alive returns HTTP 409 with the existing
run's info — NEVER a duplicate launch. A DEAD recorded pid self-heals (the
lock is recoverable) — stale-pid recovery is logged, not silent.

Honest envelope (Golden Rule #10): pid liveness is a *probe*
(``waitpid(WNOHANG)`` for our own children — which also reaps the zombie a
detached-but-still-our-child exit leaves — falling back to ``kill(pid, 0)``
after a dashboard restart re-parents the run), not a supervisor; a recycled
pid after months could in principle read "running" (started_utc is recorded
for the operator to judge). The log tail is a bounded seek-read of the last N
KiB, never a whole-file read. ``fcntl`` is POSIX (Linux + macOS — the Track-1
envelope); ``caffeinate`` wrapping degrades gracefully where absent.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import subprocess  # nosec B404 — fixed argv, no shell, launches the repo's own CLI only
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from brutex.calendars.nse import now_ist
from brutex.data.timeframes import LAKE_INTERVAL_LABELS

if TYPE_CHECKING:
    from collections.abc import Callable

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "DEFAULT_MIN_DISK_FREE_BYTES",
    "FUTURES_DEPENDENCY_NOTE",
    "GRID_HONESTY_NOTE",
    "LAUNCH_CARDS",
    "LOCKED_DIRECTIONS",
    "LOCKED_INTERVALS",
    "LOCKED_K",
    "LOCKED_MAX_COMBOS",
    "LOCKED_START",
    "LOCKED_SWEEP_WORKERS",
    "LOCKED_TOP",
    "ActiveLaunch",
    "CardSpec",
    "FuturesAvailability",
    "LaunchOutcome",
    "PreconditionCheck",
    "build_launch_state",
    "launch_end_date",
    "launch_lock_path",
    "launch_state_path",
    "launch_status",
    "launch_sweep",
    "locked_sweep_argv",
    "probe_futures_alias",
    "probe_pid",
    "read_launch_state",
    "read_log_tail",
    "register_launch_routes",
    "run_preconditions",
    "store_lake_root_resolver",
    "wrap_keepawake",
]

# ── Locked run shape (operator-locked 2026-07-08 for tonight's launch) ──

#: Locked window start — the data-pull floor (CLAUDE.md §2 Rule 3).
LOCKED_START: Final[str] = "2020-01-01"

#: All 21 lake timeframes, minute-ascending — imported from the ONE source of
#: truth (:data:`~brutex.data.timeframes.LAKE_INTERVAL_LABELS`), never re-minted.
LOCKED_INTERVALS: Final[tuple[str, ...]] = tuple(
    LAKE_INTERVAL_LABELS[minutes] for minutes in sorted(LAKE_INTERVAL_LABELS)
)

#: Locked directions / combo depths / guards — passed EXPLICITLY even where a
#: value is the CLI default (dedup-identity hygiene: the launch command is the
#: complete record of the run shape).
LOCKED_DIRECTIONS: Final[str] = "BUY,SELL"
LOCKED_K: Final[str] = "1,2,3,4,5,6"
LOCKED_SL: Final[str] = "none"
LOCKED_TP: Final[str] = "none"
LOCKED_MAX_COMBOS: Final[int] = 50_100_000
LOCKED_TOP: Final[int] = 20

#: Worker count exported to the detached CLI process (CLAUDE.md §8 envelope).
LOCKED_SWEEP_WORKERS: Final[str] = "8"

#: The honest grid summary — the futures ``1day`` TF is structurally
#: TRADE-less (audit: phase3/W_1264_branch_validation.md), so the page never
#: implies 21 productive timeframes.
GRID_HONESTY_NOTE: Final[str] = "20 trade-bearing TFs + 1day (no trades by design)"

#: The honest futures-card dependency note shown while the alias is absent.
FUTURES_DEPENDENCY_NOTE: Final[str] = "futures layer not merged yet — PR #1264"

#: The whole-series futures symbol PR #1264's alias layer resolves.
FUTURES_SYMBOL: Final[str] = "NSE-NIFTY-FUT"

#: Disk-free floor for the precondition check (a multi-day sweep writes
#: per-tuple Parquet + checkpoints; 20 GiB is a conservative cheap gate,
#: injectable per call).
DEFAULT_MIN_DISK_FREE_BYTES: Final[int] = 20 * 1024**3

#: Bounded log-tail read (last N KiB via seek — never a whole-file read).
DEFAULT_LOG_TAIL_BYTES: Final[int] = 16 * 1024


@dataclass(frozen=True, slots=True)
class CardSpec:
    """One launch card's locked coordinates (futures-primary / spot-fallback)."""

    card: str
    title: str
    symbol: str
    exchange: str
    segment: str


#: The two launch cards — the POST whitelist (no user string beyond this
#: selector ever reaches the command line).
LAUNCH_CARDS: Final[dict[str, CardSpec]] = {
    "futures": CardSpec(
        card="futures",
        title="NIFTY front-month futures (primary)",
        symbol=FUTURES_SYMBOL,
        exchange="NSE",
        segment="FNO",
    ),
    "spot": CardSpec(
        card="spot",
        title="NSE-NIFTY spot (fallback)",
        symbol="NIFTY",
        exchange="NSE",
        segment="INDEX",
    ),
}


def launch_end_date(today: date) -> date:
    """Return the locked ``--end`` value: ``today + 1 day``.

    A bare-date ``--end`` EXCLUDES that day's session on the futures path
    (audit evidence: phase3/W_1264_branch_validation.md), so the locked
    command passes tomorrow's date — the run then includes today's session
    and can never silently drop the final session.
    """
    return today + timedelta(days=1)


def locked_sweep_argv(
    card: str,
    *,
    lake_root: Path,
    results_root: Path,
    end_day: date,
    python_executable: str | None = None,
) -> list[str]:
    """Build the LOCKED ``brutex sweep-all`` argv for *card* (pure, list-args).

    Every flag exists on main's ``cli.py`` (verified against the real argparse
    definitions). ``--acknowledge-sla-overrun`` is the real SLA-guard
    acknowledgment flag (DEC-SLA-LAUNCH-GUARD-001) — the full 21-TF k≤6 grid
    legitimately projects past the §8 2-4-day envelope and the operator has
    locked the run anyway. ``--lake-root`` / ``--results-root`` are passed
    explicitly so the detached process never depends on inherited env.

    Raises
    ------
    ValueError
        On an unknown *card* (defence in depth — the route validates first).
    """
    spec = LAUNCH_CARDS.get(card)
    if spec is None:
        raise ValueError(f"unknown launch card {card!r}: expected one of {sorted(LAUNCH_CARDS)}")
    python = python_executable if python_executable is not None else sys.executable
    return [
        python,
        "-m",
        "brutex.cli",
        "sweep-all",
        "--symbols",
        spec.symbol,
        "--exchange",
        spec.exchange,
        "--segment",
        spec.segment,
        "--start",
        LOCKED_START,
        "--end",
        end_day.isoformat(),
        "--intervals",
        ",".join(LOCKED_INTERVALS),
        "--directions",
        LOCKED_DIRECTIONS,
        "--k",
        LOCKED_K,
        "--sl",
        LOCKED_SL,
        "--tp",
        LOCKED_TP,
        "--max-combos",
        str(LOCKED_MAX_COMBOS),
        "--top",
        str(LOCKED_TOP),
        "--results-root",
        str(results_root),
        "--lake-root",
        str(lake_root),
        "--acknowledge-sla-overrun",
    ]


def wrap_keepawake(
    argv: list[str],
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Wrap *argv* in ``caffeinate -i -s`` when the binary exists (macOS).

    Degrades gracefully on Linux/CI (no caffeinate → the argv is returned
    unchanged) — the launch never fails for a missing power-assertion binary.
    """
    caffeinate = which("caffeinate")
    if caffeinate is None:
        return list(argv)
    return [caffeinate, "-i", "-s", *argv]


# ── Futures-availability probe (PR #1264 alias layer) ──


@dataclass(frozen=True, slots=True)
class FuturesAvailability:
    """The dynamic request-time verdict on the ``NSE-NIFTY-FUT`` alias."""

    available: bool
    reason: str


def probe_futures_alias() -> FuturesAvailability:
    """Probe whether the ``NSE-NIFTY-FUT`` alias resolves in the engine scope.

    Calls the REAL scope classifier
    (:func:`brutex.universe.classify_engine_sweep_target`) at request time —
    never a hardcoded assumption. On main (pre-#1264) the alias is refused;
    the moment PR #1264's alias layer merges (+ dashboard restart) this reads
    available with zero code change here. A missing/reshaped classifier
    (guarded import) degrades to "unavailable", never a crash.
    """
    try:
        universe = importlib.import_module("brutex.universe")
        classify = universe.classify_engine_sweep_target
    except (ImportError, AttributeError) as exc:
        return FuturesAvailability(
            available=False,
            reason=f"scope classifier unavailable ({exc.__class__.__name__}) — {FUTURES_DEPENDENCY_NOTE}",
        )
    verdict = classify(exchange="NSE", segment="FNO", symbol=FUTURES_SYMBOL)
    if bool(verdict.allowed):
        return FuturesAvailability(available=True, reason=str(verdict.reason))
    return FuturesAvailability(
        available=False,
        reason=f"{FUTURES_DEPENDENCY_NOTE} ({verdict.reason})",
    )


# ── Precondition checks (cheap, page-load only — no heavy scans) ──


@dataclass(frozen=True, slots=True)
class PreconditionCheck:
    """One cheap server-side launch precondition verdict."""

    name: str
    ok: bool
    detail: str


def _wheels_check(
    *,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> PreconditionCheck:
    """Both Rust wheels importable (``fno_math`` + ``trading_core``).

    *find_spec* is injectable so tests drive the missing/broken-wheel arms
    without uninstalling a real wheel; a raising probe (broken package)
    honestly counts as missing, never a crash on the page.
    """
    missing: list[str] = []
    for wheel in ("fno_math", "trading_core"):
        try:
            spec = find_spec(wheel)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(wheel)
    if missing:
        return PreconditionCheck(
            name="rust_wheels",
            ok=False,
            detail=f"missing wheel(s): {', '.join(missing)} — run maturin develop",
        )
    return PreconditionCheck(
        name="rust_wheels", ok=True, detail="fno_math + trading_core importable"
    )


def run_preconditions(
    lake_root: Path | None,
    *,
    min_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
) -> list[PreconditionCheck]:
    """Run the cheap launch preconditions (bounded — a few stats, zero scans).

    Checks: lake configured · NIFTY spot 1-minute partition present · disk
    free ≥ *min_free_bytes* · Rust wheels importable · sweeps root writable ·
    no launch already active. Deep lake-completeness checks are deliberately
    NOT duplicated here — the launched CLI itself refuses loudly on missing
    slices (DEC-SWEEP-PREFLIGHT-001).
    """
    checks: list[PreconditionCheck] = []
    if lake_root is None:
        checks.append(
            PreconditionCheck(name="lake_configured", ok=False, detail="no lake configured")
        )
        checks.append(_wheels_check())
        return checks
    checks.append(PreconditionCheck(name="lake_configured", ok=True, detail=str(lake_root)))

    nifty_1m = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    checks.append(
        PreconditionCheck(
            name="nifty_1minute_partition",
            ok=nifty_1m.is_dir(),
            detail=str(nifty_1m),
        )
    )

    try:
        free = int(disk_usage(str(lake_root)).free)
        ok = free >= min_free_bytes
        detail = f"{free / 1024**3:.1f} GiB free (floor {min_free_bytes / 1024**3:.1f} GiB)"
    except OSError as exc:
        ok = False
        detail = f"disk_usage failed: {exc}"
    checks.append(PreconditionCheck(name="disk_free", ok=ok, detail=detail))

    checks.append(_wheels_check())

    sweeps_dir = lake_root / "sweeps"
    writable_probe = sweeps_dir if sweeps_dir.is_dir() else lake_root
    checks.append(
        PreconditionCheck(
            name="results_root_writable",
            ok=os.access(writable_probe, os.W_OK),
            detail=str(writable_probe),
        )
    )

    state = read_launch_state(lake_root)
    if state is None:
        checks.append(
            PreconditionCheck(name="no_active_launch", ok=True, detail="no recorded launch")
        )
    else:
        running = probe_pid(_state_pid(state)) == "running"
        checks.append(
            PreconditionCheck(
                name="no_active_launch",
                ok=not running,
                detail=(
                    f"pid {state.get('pid')} ({state.get('card')}) "
                    + ("is RUNNING" if running else "exited — lock recoverable")
                ),
            )
        )
    return checks


# ── Single-flight launch state (flock sidecar + pidfile, stale-pid self-heal) ──


def launch_lock_path(lake_root: Path) -> Path:
    """The stable flock sidecar guarding the launch critical section.

    Under ``<lake_root>/.locks/`` — outside ``bars/``/``options/`` and not a
    ``*.parquet``, so no lake scanner ever sees it (the
    DEC-LAKE-PARTITION-FLOCK-001 sidecar convention).
    """
    return lake_root / ".locks" / "sweep_launch_button.lock"


def launch_state_path(lake_root: Path) -> Path:
    """The active-launch pidfile (JSON) under the sweeps tree."""
    return lake_root / "sweeps" / ".launch" / "active_launch.json"


def _state_pid(state: dict[str, Any]) -> int:
    """Coerce the recorded pid; junk (out-of-band edit) reads ``-1`` = dead."""
    try:
        return int(state.get("pid", -1))
    except (TypeError, ValueError):
        return -1


def probe_pid(pid: int) -> Literal["running", "exited"]:
    """Best-effort liveness probe for a recorded launch pid.

    ``waitpid(WNOHANG)`` first — for a pid that is OUR child this both
    detects exit AND reaps the zombie a detached-but-unwaited child leaves
    (``kill(pid, 0)`` alone would read a zombie as alive forever). After a
    dashboard restart the run is no longer our child (``ChildProcessError``)
    and the guarded ``kill(pid, 0)`` probe takes over. Honest limit: a
    recycled pid can read "running" — ``started_utc`` is recorded so the
    operator can judge.
    """
    if pid <= 0:
        return "exited"
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        return "exited" if reaped == pid else "running"
    except ChildProcessError:
        pass  # not our child (e.g. dashboard restarted) — fall through
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "exited"
    except PermissionError:
        return "running"
    return "running"


def read_launch_state(lake_root: Path) -> dict[str, Any] | None:
    """Read the pidfile; a missing/corrupt file is honestly ``None`` (logged).

    The pidfile is written atomically (tmp + ``os.replace``), so corruption
    implies an out-of-band edit or a torn volume — logged, never a crash and
    never a permanent launch wedge (the flock still serialises the critical
    section, so self-healing a corrupt record cannot double-launch a LIVE
    run that keeps its pid recorded elsewhere: the record IS the registry).
    """
    path = launch_state_path(lake_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning("launch_button.state_unreadable", path=str(path), error=str(exc))
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.warning("launch_button.state_corrupt", path=str(path), error=str(exc))
        return None
    if not isinstance(state, dict):
        _LOG.warning("launch_button.state_not_object", path=str(path))
        return None
    return state


def _write_launch_state(lake_root: Path, state: dict[str, Any]) -> None:
    """Atomically persist the pidfile (tmp + ``os.replace``)."""
    path = launch_state_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True, slots=True)
class ActiveLaunch:
    """The recorded active launch (mirrors the pidfile payload)."""

    pid: int
    card: str
    results_root: str
    log_path: str
    command: tuple[str, ...]
    started_utc: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready projection."""
        return {
            "pid": self.pid,
            "card": self.card,
            "results_root": self.results_root,
            "log_path": self.log_path,
            "command": list(self.command),
            "started_utc": self.started_utc,
        }


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    """The verdict of one POST launch attempt."""

    started: bool
    status: Literal["started", "already_running", "lock_contended"]
    active: dict[str, Any] | None


def _default_spawner(argv: list[str], log_path: Path) -> int:
    """Spawn *argv* fully detached; stdout+stderr append to *log_path*.

    ``start_new_session=True`` + no ``.wait()`` + no owning thread — the
    dashboard never supervises the run; a restart cannot kill it. The child
    inherits the environment plus ``BRUTEX_SWEEP_WORKERS`` (the §8 envelope).
    """
    with log_path.open("ab") as log_fh:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 — whitelist-built argv, no shell, no user strings
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "BRUTEX_SWEEP_WORKERS": LOCKED_SWEEP_WORKERS},
        )
    pid = proc.pid
    # DELIBERATE disown: the dashboard never owns/waits this handle (that is
    # the whole point — a restart must not kill the run), and liveness is the
    # pidfile probe (whose waitpid(WNOHANG) reaps the eventual zombie). Mark
    # the Popen handle finished so its finalizer never emits the misleading
    # "subprocess is still running" ResourceWarning at GC.
    proc.returncode = 0
    return pid


def launch_sweep(
    card: str,
    *,
    lake_root: Path,
    spawner: Callable[[list[str], Path], int] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    now_utc: Callable[[], datetime] | None = None,
    today_ist: Callable[[], date] | None = None,
) -> LaunchOutcome:
    """Launch the locked sweep for *card* under the single-flight flock.

    Double-click-proof: the flock (``LOCK_EX | LOCK_NB``) serialises the
    critical section — a concurrent second click reads ``lock_contended``;
    a click while the recorded pid is ALIVE reads ``already_running``. A
    DEAD recorded pid self-heals (logged) and the launch proceeds. Only the
    validated card selector parameterises the command (whitelist —
    :data:`LAUNCH_CARDS`); no user-controlled string reaches the argv.

    Raises
    ------
    ValueError
        On an unknown *card* (the route validates first; defence in depth).
    """
    if card not in LAUNCH_CARDS:
        raise ValueError(f"unknown launch card {card!r}: expected one of {sorted(LAUNCH_CARDS)}")
    spawn = spawner if spawner is not None else _default_spawner
    clock = now_utc if now_utc is not None else lambda: datetime.now(UTC)
    today_fn = today_ist if today_ist is not None else lambda: now_ist().date()

    lock_path = launch_lock_path(lake_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _LOG.info("launch_button.lock_contended", card=card)
            return LaunchOutcome(
                started=False, status="lock_contended", active=read_launch_state(lake_root)
            )

        state = read_launch_state(lake_root)
        if state is not None:
            pid = _state_pid(state)
            if probe_pid(pid) == "running":
                _LOG.info("launch_button.already_running", card=card, active_pid=pid)
                return LaunchOutcome(started=False, status="already_running", active=state)
            _LOG.info(
                "launch_button.stale_pid_self_heal",
                stale_pid=pid,
                stale_card=state.get("card"),
            )

        started = clock()
        stamp = started.strftime("%Y%m%d-%H%M%S")
        results_root = lake_root / "sweeps" / f"launch-{stamp}-{card}"
        results_root.mkdir(parents=True, exist_ok=True)
        log_path = results_root / "launch.log"
        argv = wrap_keepawake(
            locked_sweep_argv(
                card,
                lake_root=lake_root,
                results_root=results_root,
                end_day=launch_end_date(today_fn()),
            ),
            which=which,
        )
        pid = spawn(argv, log_path)
        active = ActiveLaunch(
            pid=pid,
            card=card,
            results_root=str(results_root),
            log_path=str(log_path),
            command=tuple(argv),
            started_utc=started.isoformat(),
        )
        _write_launch_state(lake_root, active.as_dict())
        _LOG.info(
            "launch_button.sweep_launched",
            card=card,
            pid=pid,
            results_root=str(results_root),
        )
        return LaunchOutcome(started=True, status="started", active=active.as_dict())
    finally:
        os.close(lock_fd)  # closing the fd releases the flock


# ── Status / page-state projections ──


def read_log_tail(log_path: Path, *, tail_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> str:
    """Bounded tail of *log_path* — seek to the last *tail_bytes*, never a full read."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            return fh.read(tail_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def launch_status(
    lake_root: Path | None, *, tail_bytes: int = DEFAULT_LOG_TAIL_BYTES
) -> dict[str, Any]:
    """The poll payload: pidfile liveness + bounded log tail."""
    if lake_root is None:
        return {"present": False, "running": False}
    state = read_launch_state(lake_root)
    if state is None:
        return {"present": False, "running": False}
    pid = _state_pid(state)
    running = probe_pid(pid) == "running"
    log_path = state.get("log_path")
    tail = read_log_tail(Path(log_path), tail_bytes=tail_bytes) if isinstance(log_path, str) else ""
    return {
        "present": True,
        "running": running,
        "pid": pid,
        "card": state.get("card"),
        "results_root": state.get("results_root"),
        "log_path": log_path,
        "started_utc": state.get("started_utc"),
        "command": state.get("command"),
        "log_tail": tail,
    }


def _card_payload(
    spec: CardSpec,
    *,
    lake_root: Path | None,
    end_day: date,
    futures: FuturesAvailability,
    which: Callable[[str], str | None],
) -> dict[str, Any]:
    """One card's page projection: locked command preview + enabled verdict."""
    enabled = lake_root is not None
    reason = "" if enabled else "no lake configured"
    if spec.card == "futures":
        enabled = enabled and futures.available
        if not futures.available:
            reason = futures.reason
    command = wrap_keepawake(
        locked_sweep_argv(
            spec.card,
            lake_root=lake_root if lake_root is not None else Path("<lake-root>"),
            results_root=Path("<fresh-timestamped-results-root>"),
            end_day=end_day,
        ),
        which=which,
    )
    return {
        "card": spec.card,
        "title": spec.title,
        "symbol": spec.symbol,
        "exchange": spec.exchange,
        "segment": spec.segment,
        "enabled": enabled,
        "reason": reason,
        "command": command,
        "command_display": " ".join(command),
    }


def build_launch_state(
    lake_root: Path | None,
    *,
    min_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
    which: Callable[[str], str | None] = shutil.which,
    today_ist: Callable[[], date] | None = None,
) -> dict[str, Any]:
    """The ONE page-state payload the frontend polls (~2.5 s).

    Preconditions + futures probe + both locked command previews + the active
    launch (with bounded log tail). Everything is derived server-side; the
    frontend renders via ``textContent`` only.
    """
    today_fn = today_ist if today_ist is not None else lambda: now_ist().date()
    end_day = launch_end_date(today_fn())
    futures = probe_futures_alias()
    return {
        "interim": True,
        "lake_root": str(lake_root) if lake_root is not None else None,
        "end_day": end_day.isoformat(),
        "grid_note": GRID_HONESTY_NOTE,
        "preconditions": [
            {"name": c.name, "ok": c.ok, "detail": c.detail}
            for c in run_preconditions(lake_root, min_free_bytes=min_free_bytes)
        ],
        "futures": {"available": futures.available, "reason": futures.reason},
        "cards": {
            name: _card_payload(
                spec, lake_root=lake_root, end_day=end_day, futures=futures, which=which
            )
            for name, spec in LAUNCH_CARDS.items()
        },
        "active": launch_status(lake_root),
    }


# ── Route registration (the ONE additive build_app hunk calls this) ──


def store_lake_root_resolver(store: Any) -> Callable[[], Path | None]:
    """Build the lake-root resolver from the app's instrument-store handle.

    Mirrors the app.py route convention (``store.path.parent`` is the lake
    dir); a ``None``/pathless store resolves ``None`` — the page still
    renders, the POST refuses with 503.
    """

    def _resolve() -> Path | None:
        path = getattr(store, "path", None)
        return path.parent if isinstance(path, Path) else None

    return _resolve


_LAUNCH_TEMPLATE_PATH: Final[Path] = Path(__file__).resolve().parent / "templates" / "launch.html"
_LAUNCH_TEMPLATE_CACHE: str | None = None


def _load_launch_template() -> str:
    """Load + cache the INTERIM launch page template (served verbatim)."""
    global _LAUNCH_TEMPLATE_CACHE  # noqa: PLW0603 — single-load module cache (house pattern)
    if _LAUNCH_TEMPLATE_CACHE is None:
        _LAUNCH_TEMPLATE_CACHE = _LAUNCH_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _LAUNCH_TEMPLATE_CACHE


def register_launch_routes(
    app: Any,
    *,
    lake_root_resolver: Callable[[], Path | None],
    spawner: Callable[[list[str], Path], int] | None = None,
    min_free_bytes: int = DEFAULT_MIN_DISK_FREE_BYTES,
) -> None:
    """Register the INTERIM launch page + API routes on *app* (additive only).

    FastAPI is imported lazily (the app.py doctrine — unit tests can import
    this module without the ``[ui]`` extra). *spawner* is the injectable
    detached-process seam so tests NEVER launch a real sweep.
    """
    from fastapi import HTTPException, Query
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import HTMLResponse, JSONResponse

    async def launch_page() -> str:
        """GET /launch — the INTERIM Run-Sweep launch page."""
        return _load_launch_template()

    async def api_launch_state() -> JSONResponse:
        """GET /api/v1/launch/state — preconditions + cards + active-run poll."""
        lake_root = lake_root_resolver()
        payload = await run_in_threadpool(
            build_launch_state, lake_root, min_free_bytes=min_free_bytes
        )
        return JSONResponse(payload)

    async def api_launch_sweep(card: str = Query(...)) -> JSONResponse:
        """POST /api/v1/launch/sweep?card=futures|spot — single-flight detached launch."""
        if card not in LAUNCH_CARDS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown card {card!r}: expected one of {sorted(LAUNCH_CARDS)}",
            )
        lake_root = lake_root_resolver()
        if lake_root is None:
            raise HTTPException(status_code=503, detail="no lake configured")
        if card == "futures":
            futures = probe_futures_alias()
            if not futures.available:
                raise HTTPException(status_code=400, detail=futures.reason)
        outcome = await run_in_threadpool(launch_sweep, card, lake_root=lake_root, spawner=spawner)
        payload = {"started": outcome.started, "status": outcome.status, "active": outcome.active}
        return JSONResponse(payload, status_code=200 if outcome.started else 409)

    # Registered by CALL (not decorator syntax) so `mypy --strict` never trips
    # its untyped-decorator check on the untyped FastAPI decorator factories —
    # byte-equivalent registration (a decorator IS this call).
    app.get("/launch", response_class=HTMLResponse)(launch_page)
    app.get("/api/v1/launch/state")(api_launch_state)
    app.post("/api/v1/launch/sweep")(api_launch_sweep)

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
honest ``--max-combos`` guard, and (c) refuse
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
Under the held flock a "launching" placeholder record is written atomically
BEFORE the spawn; immediately after ``Popen`` returns, the placeholder is
atomically REWRITTEN with the real pid (still phase ``launching`` — round-2
refuter FIX-A), then finalized (review round-2 M2). A failed spawn removes
the placeholder (no 120 s lockout — FIX-B); a failed pid-stamp/finalize
write KILLS the just-spawned session (compensation). A pid-BEARING
launching record is judged by PROBING its pid; ONLY a pid-less placeholder
self-heals by age. HONEST residual (Golden Rule #10): a dashboard death in
the tiny window between ``Popen`` returning and the pid-stamp write still
strands a pid-less placeholder over a live sweep — after the 120 s grace it
self-heals and a duplicate launch becomes possible; FIX-A SHRINKS that
window from spawn+watcher+finalize to a single atomic write, it does NOT
vanish. A second click while the run is alive returns HTTP 409 with the
existing run's info — never a duplicate launch on any probed-alive record.
A DEAD recorded pid self-heals (the lock is recoverable) — stale-pid
recovery is logged, not silent. The pidfile ALWAYS holds the REAL sweep pid
(review round-2 M1): the sweep is spawned directly, and keep-awake is a
SEPARATE detached ``caffeinate -dimsu -w <sweep_pid>`` watcher whose death is
harmless — it never carries run identity and self-exits when the sweep ends.

SLA guard (coordinator delta): ``--acknowledge-sla-overrun`` is NOT
unconditional — the audit expects the locked 252-tuple grid to PASS
DEC-SLA-LAUNCH-GUARD-001 without it. The page carries an off-by-default
acknowledge checkbox; the POST accepts a strict whitelisted boolean and
appends the flag ONLY when ticked (the operator's deliberate override,
never a default).

Honest envelope (Golden Rule #10): pid liveness is a *probe*
(``waitpid(WNOHANG)`` for our own children — which also reaps the zombie a
detached-but-still-our-child exit leaves — falling back to ``kill(pid, 0)``
after a dashboard restart re-parents the run), not a supervisor; recycled-pid
disambiguation is best-effort ``/proc/<pid>/cmdline`` on Linux (absent on
macOS — skipped silently; a probe ERROR never downgrades a live run to
exited, the safe direction is refusing a duplicate). The log tail is a
bounded seek-read of the last N KiB, confined under ``<lake_root>/sweeps``
(a tampered state file cannot read arbitrary files). The lake precondition
checks PRESENCE of all 21 NIFTY/INDEX interval partitions; TF FRESHNESS is
deliberately NOT re-derived here — the launched CLI's own preflight verifies
it at launch (a refusal appears in the log tail within seconds). The page
ASSUMES a SINGLE dashboard process — liveness probing is waitpid/kill-based,
so multi-worker uvicorn or reparenting can read a finished sweep as running
until its parent reaps it (safe direction: refuses duplicates, never
launches one). Pid-stamp-write-failure asymmetry (R3 MINOR-1, judged not
practically reachable): on a pid-stamp ``OSError`` the durable record
reverts to the pid-less placeholder, so if the compensating ``killpg`` ALSO
failed against a still-live child, the 120 s age-out could permit a
duplicate — surfaced to the operator as the 500 "verify manually" detail.
The launch does NOT coordinate with an in-flight pull/prebuild on the same
lake (stated on-page — ensure no pull is running before clicking).
``fcntl`` is POSIX (Linux + macOS — the Track-1 envelope); the keep-awake
watcher degrades gracefully where ``caffeinate`` is absent. Deferred to the
canonical page (disclosed, not implemented): flock critical-section
deadline-bounding, minimal-env spawn, auth on the launch route, disk-floor
vs true sweep footprint sizing.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess  # nosec B404 — fixed argv, no shell, launches the repo's own CLI only
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from brutex.calendars.nse import now_ist
from brutex.data.atomic_io import atomic_write_simple
from brutex.data.timeframes import LAKE_INTERVAL_LABELS

if TYPE_CHECKING:
    from collections.abc import Callable

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "DEFAULT_MIN_DISK_FREE_BYTES",
    "FUTURES_DEPENDENCY_NOTE",
    "GRID_HONESTY_NOTE",
    "K6_FOLLOWON_NOTE",
    "LAUNCH_CARDS",
    "LOCKED_DIRECTIONS",
    "LOCKED_INTERVALS",
    "LOCKED_K",
    "LOCKED_MAX_COMBOS",
    "LOCKED_STALL_MASK_BARS_PER_SEC",
    "LOCKED_START",
    "LOCKED_SWEEP_MEM_BUDGET_BYTES",
    "LOCKED_SWEEP_WORKERS",
    "LOCKED_TOP",
    "SLA_ACK_FLAG",
    "ActiveLaunch",
    "CardSpec",
    "FuturesAvailability",
    "LaunchOutcome",
    "PreconditionCheck",
    "build_launch_state",
    "keepawake_watcher_argv",
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
    "state_liveness",
    "store_lake_root_resolver",
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
LOCKED_K: Final[str] = "1,2,3,4,5"  # audit delta: k=6 dropped — measured per-tuple
# timing (W05_stall_table) puts k<=6 at 4.2-13.9 M4-days, outside the launch SLA;
# k=6 runs as a documented follow-on delta run (identical flags, same --max-combos).
LOCKED_SL: Final[str] = "none"
LOCKED_TP: Final[str] = "none"
LOCKED_MAX_COMBOS: Final[int] = 50_100_000
LOCKED_TOP: Final[int] = 20

#: Worker count exported to the detached CLI process (CLAUDE.md §8 envelope).
LOCKED_SWEEP_WORKERS: Final[str] = "8"

#: Rank-region byte budget exported to the child (audit delta, W07_m4_oom_table:
#: the divisor=1 worker OOM was live-proven and the 1 GiB arm bit-identical).
#: Verified against main's real env name (`sweep_mem_budget.MEM_BUDGET_ENV`) and
#: its fail-loud positive-int parser (`resolve_mem_budget_bytes` accepts this
#: literal).
LOCKED_SWEEP_MEM_BUDGET_BYTES: Final[str] = "1073741824"

#: Stall-guard floor exported to the child (audit delta, W05_stall_table:
#: pre-calibrated so the #1291 stall guard, once merged, does not false-abort
#: heavy 1-minute tuples). HONEST: #1291 is UNMERGED — main reads no such env
#: (grep-verified), so this is inert-but-harmless until it lands.
LOCKED_STALL_MASK_BARS_PER_SEC: Final[str] = "20000000"

#: The honest on-page k=6 note (audit delta — kept OUT of tonight's locked grid).
K6_FOLLOWON_NOTE: Final[str] = "k=6 runs as a documented follow-on delta run"

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

#: The REAL SLA-guard acknowledgment flag (DEC-SLA-LAUNCH-GUARD-001).
#: OPT-IN ONLY (coordinator delta): the audit expects the locked 252-tuple
#: grid to PASS the guard WITHOUT it — the page's off-by-default checkbox is
#: the operator's deliberate override, never a default.
SLA_ACK_FLAG: Final[str] = "--acknowledge-sla-overrun"

#: How long a pre-spawn "launching" placeholder record counts as a live
#: launch before it self-heals as stranded (a crash between the placeholder
#: write and the pid finalize — review round-2 M2). Spawn + finalize take
#: milliseconds; 120 s is a generous crash-detection grace.
LAUNCHING_GRACE_S: Final[float] = 120.0


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
    acknowledge_sla: bool = False,
) -> list[str]:
    """Build the LOCKED ``brutex sweep-all`` argv for *card* (pure, list-args).

    Every flag exists on main's ``cli.py`` (verified against the real argparse
    definitions). ``--lake-root`` / ``--results-root`` are passed explicitly
    so the detached process never depends on inherited env.
    :data:`SLA_ACK_FLAG` is appended ONLY when *acknowledge_sla* is True
    (coordinator delta — the audit expects the locked 252-tuple grid to PASS
    DEC-SLA-LAUNCH-GUARD-001 without the acknowledgment; the checkbox is the
    operator's deliberate override, not a default).

    Raises
    ------
    ValueError
        On an unknown *card* (defence in depth — the route validates first).
    """
    spec = LAUNCH_CARDS.get(card)
    if spec is None:
        raise ValueError(f"unknown launch card {card!r}: expected one of {sorted(LAUNCH_CARDS)}")
    python = python_executable if python_executable is not None else sys.executable
    argv = [
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
    ]
    if acknowledge_sla:
        argv.append(SLA_ACK_FLAG)
    return argv


def keepawake_watcher_argv(
    sweep_pid: int,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str] | None:
    """The SEPARATE keep-awake watcher argv, or ``None`` where unavailable.

    Review round-2 M1 + audit delta (W02_keepawake): keep-awake is
    ``caffeinate -dimsu -w <sweep_pid>`` — a
    DETACHED WATCHER of the real sweep pid, never a wrapper carrying run
    identity. Its death is harmless (the pidfile keys on the sweep's own
    pid) and it self-exits when the sweep ends (``-w``). Degrades gracefully
    on Linux/CI (no caffeinate → ``None``, launch proceeds).
    """
    caffeinate = which("caffeinate")
    if caffeinate is None:
        return None
    return [caffeinate, "-dimsu", "-w", str(sweep_pid)]


def _default_watcher_spawner(argv: list[str]) -> int:
    """Spawn the keep-awake watcher fully detached (all streams devnull)."""
    proc = subprocess.Popen(  # noqa: S603  # nosec B603 — fixed caffeinate argv, no shell, no user strings
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = proc.pid
    # Disown (mirrors _default_spawner): -w self-exits with the sweep; the
    # dashboard never waits it — mark finished so GC never warns.
    proc.returncode = 0
    return pid


def _spawn_keepawake_watcher(
    sweep_pid: int,
    *,
    which: Callable[[str], str | None],
    watcher_spawner: Callable[[list[str]], int] | None = None,
) -> int | None:
    """Best-effort keep-awake watcher spawn — NEVER fails the launch."""
    argv = keepawake_watcher_argv(sweep_pid, which=which)
    if argv is None:
        return None
    spawn = watcher_spawner if watcher_spawner is not None else _default_watcher_spawner
    try:
        watcher_pid = spawn(argv)
    except OSError as exc:
        _LOG.warning("launch_button.keepawake_spawn_failed", sweep_pid=sweep_pid, error=str(exc))
        return None
    _LOG.info("launch_button.keepawake_spawned", sweep_pid=sweep_pid, watcher_pid=watcher_pid)
    return watcher_pid


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

    # Review round-2 M4: the locked 21-TF command (no --allow-missing-slices)
    # hard-refuses via the CLI's own preflight if ANY interval partition is
    # missing — so the cheap PRESENCE check must cover all 21, not just
    # 1minute (21 dir stats; TF FRESHNESS is deliberately NOT re-derived here
    # — the CLI preflight verifies it at launch, refusal visible in the log
    # tail within seconds).
    nifty_root = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY"
    missing_intervals = [
        interval for interval in LOCKED_INTERVALS if not (nifty_root / interval).is_dir()
    ]
    checks.append(
        PreconditionCheck(
            name="nifty_interval_partitions",
            ok=not missing_intervals,
            detail=(
                f"all {len(LOCKED_INTERVALS)} NIFTY/INDEX interval partitions present "
                f"under {nifty_root}"
                if not missing_intervals
                else (
                    f"missing {len(missing_intervals)}/{len(LOCKED_INTERVALS)} under "
                    f"{nifty_root}: {', '.join(missing_intervals)}"
                )
            ),
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
        running = state_liveness(state) == "running"
        checks.append(
            PreconditionCheck(
                name="no_active_launch",
                ok=not running,
                detail=(
                    f"pid {state.get('pid')} ({state.get('card')}, "
                    f"phase {state.get('phase', 'running')}) "
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


def _cmdline_contains_sweep_all(pid: int, *, proc_root: Path = Path("/proc")) -> bool | None:
    """Best-effort recycled-pid disambiguation via ``/proc/<pid>/cmdline``.

    Review round-2 m4. Returns ``True``/``False`` only on a DEFINITIVE read;
    ``None`` = unknown (no ``/proc`` — macOS — or unreadable/empty entry).
    The caller must treat ``None`` as "running" — a probe error must never
    downgrade a live run to exited (the safe direction is refusing a
    duplicate launch).
    """
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return b"sweep-all" in raw


def state_liveness(
    state: dict[str, Any],
    *,
    now_utc: Callable[[], datetime] | None = None,
    cmdline_probe: Callable[[int], bool | None] | None = None,
) -> Literal["running", "exited"]:
    """The ONE liveness verdict over a recorded launch state.

    * ``phase == "launching"`` WITHOUT a pid (the pre-spawn placeholder,
      review round-2 M2): counts as RUNNING within
      :data:`LAUNCHING_GRACE_S` of its ``started_utc`` (spawn + pid-stamp
      take milliseconds — a fresh placeholder means a launch is mid-flight
      and must refuse duplicates); older/junk-timestamped pid-LESS
      placeholders are STRANDED and self-heal as exited.
    * any pid-BEARING record — INCLUDING a ``launching`` record whose pid
      was stamped right after ``Popen`` returned (round-2 refuter FIX-A:
      the age-only grace NEVER applies once a pid is recorded, so a
      dashboard death before the finalize can no longer age a LIVE sweep
      into "exited" and a duplicate launch): :func:`probe_pid` on the
      recorded pid, plus the best-effort recycled-pid cmdline
      disambiguation (a DEFINITIVE non-``sweep-all`` cmdline reads exited;
      an unknown probe stays running — never a downgrade on probe error).
    """
    clock = now_utc if now_utc is not None else lambda: datetime.now(UTC)
    if state.get("phase") == "launching" and _state_pid(state) <= 0:
        try:
            started = datetime.fromisoformat(str(state.get("started_utc")))
        except ValueError:
            return "exited"  # junk placeholder timestamp — stranded, self-heal
        age_s = (clock() - started).total_seconds()
        return "running" if age_s <= LAUNCHING_GRACE_S else "exited"
    pid = _state_pid(state)
    if probe_pid(pid) != "running":
        return "exited"
    probe = cmdline_probe if cmdline_probe is not None else _cmdline_contains_sweep_all
    if probe(pid) is False:
        _LOG.info("launch_button.recycled_pid_detected", pid=pid)
        return "exited"
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
    """Atomically persist the pidfile via the COMMON atomic-write core.

    Review round-2 M3: delegates to
    :func:`~brutex.data.atomic_io.atomic_write_simple` (the guard-free
    variant — a raw ``OSError`` propagates for the caller to compensate,
    never a mis-escalated ``LakeWriteHalt``), which also fsyncs before the
    rename — closing the hand-rolled tmp+replace torn-write weakness.
    """
    path = launch_state_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True)
    atomic_write_simple(path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))


def _remove_launch_state(lake_root: Path) -> None:
    """Best-effort pidfile removal (the FIX-B spawn-failure unwind).

    Runs under the held flock, so nothing else can be writing; a removal
    failure is suppressed — the stranded pid-less placeholder then simply
    ages out via :data:`LAUNCHING_GRACE_S` (the pre-FIX-B behaviour).
    """
    with contextlib.suppress(OSError):
        launch_state_path(lake_root).unlink(missing_ok=True)


def _reap_keepawake(state: dict[str, Any]) -> None:
    """Opportunistically reap an exited keep-awake watcher (round-2 FIX-C).

    The watcher Popen handle is disowned at spawn, so its exit leaves one
    zombie per launch in the dashboard process until reaped. A guarded
    ``waitpid(WNOHANG)`` on the recorded ``keepawake_pid`` clears it from
    the status/probe path; every failure (not our child after a restart /
    already reaped / junk pid) is suppressed — this NEVER affects liveness
    verdicts (the watcher never carries run identity, M1).
    """
    raw = state.get("keepawake_pid")
    if not isinstance(raw, int) or raw <= 0:
        return
    with contextlib.suppress(OSError):
        os.waitpid(raw, os.WNOHANG)


@dataclass(frozen=True, slots=True)
class ActiveLaunch:
    """The recorded active launch (mirrors the finalized pidfile payload).

    ``pid`` is ALWAYS the real sweep pid (review round-2 M1);
    ``keepawake_pid`` is the separate best-effort caffeinate watcher —
    informational only, NEVER consulted for liveness/single-flight.
    """

    pid: int
    card: str
    results_root: str
    log_path: str
    command: tuple[str, ...]
    started_utc: str
    keepawake_pid: int | None = None
    acknowledge_sla: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready projection (the finalized ``phase="running"`` record)."""
        return {
            "phase": "running",
            "pid": self.pid,
            "card": self.card,
            "results_root": self.results_root,
            "log_path": self.log_path,
            "command": list(self.command),
            "started_utc": self.started_utc,
            "keepawake_pid": self.keepawake_pid,
            "acknowledge_sla": self.acknowledge_sla,
        }


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    """The verdict of one POST launch attempt."""

    started: bool
    status: Literal["started", "already_running", "lock_contended", "failed"]
    active: dict[str, Any] | None
    detail: str = ""


def _kill_session(pid: int) -> bool:
    """Best-effort kill of a just-spawned detached session (M2 compensation).

    The child was started with ``start_new_session=True``, so its process
    group id == its pid — ``killpg`` reaps the whole session; a raw ``kill``
    is the fallback. Returns whether a signal was delivered.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
    # Reap if it is our child so no zombie lingers (best-effort; not our
    # child / already reaped simply raises — nothing to do).
    with contextlib.suppress(OSError):
        os.waitpid(pid, 0)
    return True


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
            env={
                **os.environ,
                "BRUTEX_SWEEP_WORKERS": LOCKED_SWEEP_WORKERS,
                "BRUTEX_SWEEP_MEM_BUDGET_BYTES": LOCKED_SWEEP_MEM_BUDGET_BYTES,
                "BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC": LOCKED_STALL_MASK_BARS_PER_SEC,
            },
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
    acknowledge_sla: bool = False,
    watcher_spawner: Callable[[list[str]], int] | None = None,
) -> LaunchOutcome:
    """Launch the locked sweep for *card* under the single-flight flock.

    Double-click-proof: the flock (``LOCK_EX | LOCK_NB``) serialises the
    critical section — a concurrent second click reads ``lock_contended``;
    a click while the recorded run is ALIVE reads ``already_running``. A
    DEAD recorded pid self-heals (logged) and the launch proceeds. Only the
    validated card selector parameterises the command (whitelist —
    :data:`LAUNCH_CARDS`); no user-controlled string reaches the argv.

    M2 ordering (review round-2): under the held flock, a ``launching``
    placeholder record is written atomically BEFORE the spawn (a placeholder
    write failure spawns NOTHING); the record is finalized with the real pid
    after the spawn, and a finalize failure KILLS the just-spawned session
    (compensation) — a live run can never be invisible to single-flight. A
    crash-stranded placeholder self-heals after :data:`LAUNCHING_GRACE_S`.

    M1 (review round-2): the recorded pid is ALWAYS the real sweep pid; the
    keep-awake ``caffeinate -dimsu -w <pid>`` watcher is spawned separately,
    best-effort, and never carries run identity.

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
    # 0o600 (CodeQL alerts 862/863): the sidecar's contents are non-secret,
    # but there is no reason to be world-readable; flock semantics are
    # unaffected — same-user dashboard processes still contend on the fd.
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
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
            if state_liveness(state, now_utc=clock) == "running":
                _LOG.info(
                    "launch_button.already_running",
                    card=card,
                    active_pid=state.get("pid"),
                    active_phase=state.get("phase", "running"),
                )
                return LaunchOutcome(started=False, status="already_running", active=state)
            _LOG.info(
                "launch_button.stale_pid_self_heal",
                stale_pid=state.get("pid"),
                stale_card=state.get("card"),
                stale_phase=state.get("phase", "running"),
            )

        started = clock()
        stamp = started.strftime("%Y%m%d-%H%M%S")
        results_root = lake_root / "sweeps" / f"launch-{stamp}-{card}"
        results_root.mkdir(parents=True, exist_ok=True)
        log_path = results_root / "launch.log"
        argv = locked_sweep_argv(
            card,
            lake_root=lake_root,
            results_root=results_root,
            end_day=launch_end_date(today_fn()),
            acknowledge_sla=acknowledge_sla,
        )

        # M2 step 1: pre-spawn placeholder — atomic, BEFORE any process
        # exists. If this write fails, nothing was spawned: honest failure.
        placeholder = {
            "phase": "launching",
            "card": card,
            "results_root": str(results_root),
            "log_path": str(log_path),
            "started_utc": started.isoformat(),
        }
        try:
            _write_launch_state(lake_root, placeholder)
        except OSError as exc:
            _LOG.error("launch_button.prespawn_state_write_failed", card=card, error=str(exc))
            return LaunchOutcome(
                started=False,
                status="failed",
                active=None,
                detail=f"pre-spawn state write failed — nothing launched: {exc}",
            )

        # M2 step 2: spawn — wrapped (round-2 refuter FIX-B): a transient
        # fork/exec failure (EAGAIN/ENOMEM) must surface as an honest
        # "failed", and the just-written placeholder must NOT strand a
        # 120 s lockout when nothing was spawned.
        try:
            pid = spawn(argv, log_path)
        except OSError as exc:
            _remove_launch_state(lake_root)
            _LOG.error("launch_button.spawn_failed", card=card, error=str(exc))
            return LaunchOutcome(
                started=False,
                status="failed",
                active=None,
                detail=f"sweep spawn failed — nothing launched: {exc}",
            )

        # FIX-A (round-2 refuter): stamp the REAL pid onto the launching
        # record IMMEDIATELY after Popen returns — a dashboard death before
        # the finalize then leaves a PROBE-able record, never a pid-less
        # placeholder that ages into a duplicate launch over a live sweep.
        # (The residual window is now this single atomic write — disclosed
        # in the module docstring, it shrinks, it does not vanish.)
        try:
            _write_launch_state(lake_root, {**placeholder, "pid": pid})
        except OSError as exc:
            killed = _kill_session(pid)
            _LOG.error(
                "launch_button.pid_stamp_write_failed",
                card=card,
                pid=pid,
                compensating_kill_delivered=killed,
                error=str(exc),
            )
            return LaunchOutcome(
                started=False,
                status="failed",
                active=None,
                detail=(
                    f"pid stamp write failed after spawn: {exc}; compensating kill of "
                    f"session {pid} {'DELIVERED' if killed else 'FAILED — verify manually'}"
                ),
            )

        keepawake_pid = _spawn_keepawake_watcher(pid, which=which, watcher_spawner=watcher_spawner)

        # M2 step 3: finalize with the real pid; on failure, compensate by
        # killing the just-spawned session — never a live untracked run.
        active = ActiveLaunch(
            pid=pid,
            card=card,
            results_root=str(results_root),
            log_path=str(log_path),
            command=tuple(argv),
            started_utc=started.isoformat(),
            keepawake_pid=keepawake_pid,
            acknowledge_sla=acknowledge_sla,
        )
        try:
            _write_launch_state(lake_root, active.as_dict())
        except OSError as exc:
            killed = _kill_session(pid)
            _LOG.error(
                "launch_button.finalize_state_write_failed",
                card=card,
                pid=pid,
                compensating_kill_delivered=killed,
                error=str(exc),
            )
            return LaunchOutcome(
                started=False,
                status="failed",
                active=None,
                detail=(
                    f"pid finalize write failed after spawn: {exc}; compensating kill of "
                    f"session {pid} {'DELIVERED' if killed else 'FAILED — verify manually'}"
                ),
            )
        _LOG.info(
            "launch_button.sweep_launched",
            card=card,
            pid=pid,
            keepawake_pid=keepawake_pid,
            acknowledge_sla=acknowledge_sla,
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


def _confined_log_tail(lake_root: Path, log_path: str, *, tail_bytes: int) -> str:
    """Log tail CONFINED under ``<lake_root>/sweeps`` (review round-2 m2).

    Defence-in-depth against a tampered state file: a ``log_path`` resolving
    outside the sweeps tree is REFUSED with an honest detail — the page can
    never be steered into reading an arbitrary file.
    """
    sweeps_root = (lake_root / "sweeps").resolve()
    try:
        resolved = Path(log_path).resolve()
    except (OSError, ValueError) as exc:
        # ValueError = e.g. an embedded null byte in a tampered state file;
        # non-strict resolve() rarely raises OSError but the guard is kept.
        return f"log path unresolvable — refused ({exc.__class__.__name__})"
    if not resolved.is_relative_to(sweeps_root):
        _LOG.warning(
            "launch_button.log_tail_refused", log_path=log_path, sweeps_root=str(sweeps_root)
        )
        return f"log path outside {sweeps_root} — refused (tampered state file?)"
    return read_log_tail(resolved, tail_bytes=tail_bytes)


def launch_status(
    lake_root: Path | None, *, tail_bytes: int = DEFAULT_LOG_TAIL_BYTES
) -> dict[str, Any]:
    """The poll payload: pidfile liveness + bounded, confined log tail."""
    if lake_root is None:
        return {"present": False, "running": False}
    state = read_launch_state(lake_root)
    if state is None:
        return {"present": False, "running": False}
    _reap_keepawake(state)  # FIX-C: clear an exited watcher zombie; never affects verdicts
    pid = _state_pid(state)
    running = state_liveness(state) == "running"
    log_path = state.get("log_path")
    tail = (
        _confined_log_tail(lake_root, log_path, tail_bytes=tail_bytes)
        if isinstance(log_path, str)
        else ""
    )
    return {
        "present": True,
        "running": running,
        "phase": state.get("phase", "running"),
        "pid": pid if pid > 0 else None,
        "card": state.get("card"),
        "results_root": state.get("results_root"),
        "log_path": log_path,
        "started_utc": state.get("started_utc"),
        "command": state.get("command"),
        "keepawake_pid": state.get("keepawake_pid"),
        "log_tail": tail,
    }


def _card_payload(
    spec: CardSpec,
    *,
    lake_root: Path | None,
    end_day: date,
    futures: FuturesAvailability,
) -> dict[str, Any]:
    """One card's page projection: locked command preview + enabled verdict.

    The preview is the SWEEP command itself (unwrapped — M1: keep-awake is a
    separate watcher) WITHOUT :data:`SLA_ACK_FLAG`; the frontend appends the
    flag to the display only while the off-by-default acknowledge checkbox
    is ticked (``sla_ack_flag`` carries the exact token).
    """
    enabled = lake_root is not None
    reason = "" if enabled else "no lake configured"
    if spec.card == "futures":
        enabled = enabled and futures.available
        if not futures.available:
            reason = futures.reason
    command = locked_sweep_argv(
        spec.card,
        lake_root=lake_root if lake_root is not None else Path("<lake-root>"),
        results_root=Path("<fresh-timestamped-results-root>"),
        end_day=end_day,
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
        "sla_ack_flag": SLA_ACK_FLAG,
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
        "keepawake_available": which("caffeinate") is not None,
        "preconditions": [
            {"name": c.name, "ok": c.ok, "detail": c.detail}
            for c in run_preconditions(lake_root, min_free_bytes=min_free_bytes)
        ],
        "futures": {"available": futures.available, "reason": futures.reason},
        "cards": {
            name: _card_payload(spec, lake_root=lake_root, end_day=end_day, futures=futures)
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


#: The precondition names re-checked at POST time (review round-2 m3) — red
#: refuses with 412, nothing spawned. ``no_active_launch`` is deliberately
#: excluded (the flock + liveness single-flight is the authority → 409), as
#: is ``rust_wheels`` (this process imported them to serve the route) and
#: ``lake_configured`` (the 503 arm above).
_POST_GATE_CHECKS: Final[frozenset[str]] = frozenset(
    {"nifty_interval_partitions", "disk_free", "results_root_writable"}
)

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

    async def api_launch_sweep(
        card: str = Query(...),
        acknowledge_sla_overrun: str = Query("false"),
    ) -> JSONResponse:
        """POST /api/v1/launch/sweep?card=futures|spot — single-flight detached launch.

        ``acknowledge_sla_overrun`` is a STRICT whitelisted boolean
        (``"true"``/``"false"`` only — coordinator delta): only an explicit
        ``true`` (the page's off-by-default checkbox) appends
        :data:`SLA_ACK_FLAG` to the argv. The cheap preconditions are
        RE-CHECKED here (m3 — page-load checks alone are cosmetic for launch
        integrity): any red among the launch-gating set refuses with 412 and
        spawns nothing.
        """
        if card not in LAUNCH_CARDS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown card {card!r}: expected one of {sorted(LAUNCH_CARDS)}",
            )
        if acknowledge_sla_overrun not in ("true", "false"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"acknowledge_sla_overrun must be exactly 'true' or 'false', "
                    f"got {acknowledge_sla_overrun!r}"
                ),
            )
        acknowledge_sla = acknowledge_sla_overrun == "true"
        lake_root = lake_root_resolver()
        if lake_root is None:
            raise HTTPException(status_code=503, detail="no lake configured")
        if card == "futures":
            futures = probe_futures_alias()
            if not futures.available:
                raise HTTPException(status_code=400, detail=futures.reason)
        checks = await run_in_threadpool(
            run_preconditions, lake_root, min_free_bytes=min_free_bytes
        )
        failing = [c.name for c in checks if not c.ok and c.name in _POST_GATE_CHECKS]
        if failing:
            raise HTTPException(
                status_code=412,
                detail=f"launch preconditions failed: {', '.join(failing)} — nothing launched",
            )
        outcome = await run_in_threadpool(
            launch_sweep,
            card,
            lake_root=lake_root,
            spawner=spawner,
            acknowledge_sla=acknowledge_sla,
        )
        payload = {
            "started": outcome.started,
            "status": outcome.status,
            "active": outcome.active,
            "detail": outcome.detail,
        }
        if outcome.status == "failed":
            return JSONResponse(payload, status_code=500)
        return JSONResponse(payload, status_code=200 if outcome.started else 409)

    # Registered by CALL (not decorator syntax) so `mypy --strict` never trips
    # its untyped-decorator check on the untyped FastAPI decorator factories —
    # byte-equivalent registration (a decorator IS this call).
    app.get("/launch", response_class=HTMLResponse)(launch_page)
    app.get("/api/v1/launch/state")(api_launch_state)
    app.post("/api/v1/launch/sweep")(api_launch_sweep)

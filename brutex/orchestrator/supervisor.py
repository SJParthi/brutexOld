"""Process supervisor — auto-relaunch the sweep after a hard crash / reboot.

Closes the ONE real §11 AUTOMATED gap the sweep watchdogs left open. The two
existing watchdogs (:class:`brutex.observability.watchdog.Watchdog`,
:class:`brutex.orchestrator.watchdog.RetryStormWatchdog`) are pure *detectors* —
they DETECT a hung worker / a retry storm and fire callbacks, but nothing
RELAUNCHES the process after a hard crash / reboot. Today the operator must
re-run the command by hand. :class:`ProcessSupervisor` is the missing *actor*:
it spawns the sweep daemon as a CHILD, monitors it, and AUTO-RELAUNCHES it on an
UNEXPECTED exit (non-zero code or death-by-signal) with exponential backoff, a
max-restart cap, and a reset-backoff-after-stable-uptime window — while stopping
cleanly on a genuine success and never relaunching after an operator stop.

Because the sweep's checkpoint auto-resume (DEC-SWEEP-REALTIME-GUARANTEE-001)
already makes a relaunch bit-identical (chaos-tested), each relaunch simply
resumes from the checkpoint — so this module only has to DO the relaunch, not
re-implement resume.

## Design (mirrors the two watchdogs — DEC-WATCHDOG-001 / DEC-RATE-STORM-WATCHDOG-001)

* **Pure supervise loop / dependency-injected everything** — the child-launch
  callable, the backoff policy, the audit sink, the monotonic clock, the
  sleeper, and the "operator asked us to stop" predicate are all injected, so
  the loop is deterministic + testable with a FAKE child and an injected clock
  (no real ``subprocess`` / ``time.sleep`` in tests).
* **Caller-supplied clock + sleeper** — a monotonic-seconds callable (default
  :func:`time.monotonic`) times the child's uptime; the injected sleeper
  (default :func:`time.sleep`) waits out the backoff. Tests inject fakes and
  never sleep.
* **Caller-supplied audit sink** — every restart appends one hash-chained audit
  row (Golden Rule #8), so restarts are OBSERVABLE. A failing audit write is
  caught + logged and never crashes the supervisor (Golden Rule #10).
* **O(1) state per restart** — a small integer counter + a single last-exit
  record. A 6-month run that restarts a thousand times adds NO state; memory is
  bounded (never O(#restarts)).

## The restart decision (the whole policy in one place)

Per child life-cycle iteration:

1. launch the child (in its OWN process group), record its start time,
2. wait for it to exit with an INTERRUPTIBLE wait — poll ``child.wait_for`` in
   short slices, checking the operator-stop predicate between slices, so a
   SIGTERM/SIGINT that arrives WHILE the child is still alive is honoured
   promptly instead of blocking until the child happens to die,
3. classify the outcome:
   * **operator stop requested WHILE the child is alive** (SIGTERM/SIGINT
     reached the supervisor) → gracefully shut the child down (SIGTERM → bounded
     grace wait → SIGKILL via :meth:`shutdown_child`), STOP, do not relaunch,
   * **clean success** (exit code ``0``) → STOP, do not relaunch,
   * **operator stop that raced the child's own exit** → STOP, do not relaunch
     (the child is already gone, so no shutdown is needed),
   * **unexpected exit** (non-zero code OR death-by-signal, i.e. a negative
     ``returncode`` on POSIX) → RELAUNCH after backoff,
4. if the child had been UP longer than ``reset_after_uptime_seconds`` before it
   died, RESET the backoff (a long-lived crash is not a crash loop),
5. give up LOUDLY once ``max_restarts`` unexpected exits have happened
   (audit + non-zero supervisor exit) — a crash-loop must never spin forever.

## Honest envelope (Golden Rule #10)

* This is the CROSS-PLATFORM, cloud-testable layer. The REAL hard-reboot
  survival (relaunch after the *machine* reboots / the operator logs out) is the
  OS service manager's job — the shipped ``launchd`` (macOS ``KeepAlive``) +
  ``systemd`` (``Restart=on-failure``) templates. This supervisor survives a child
  CRASH while the supervisor itself is alive; the OS templates survive a
  supervisor / machine death. They are complementary, not redundant.
* The supervisor is single-threaded; it is NOT a scheduler and does NOT itself
  run on a cadence. Its only job is "keep exactly one child alive until it
  succeeds or the operator stops it". It waits on the child with an
  INTERRUPTIBLE poll (short ``wait_for`` slices) rather than a single blocking
  ``child.wait()`` — a bare ``os.waitpid`` is NOT interrupted by a delivered
  signal (PEP 475 auto-retries it), so an operator stop would otherwise not be
  observed until the child died on its own. Polling in slices lets the loop act
  on the stop flag promptly and terminate the child itself.

## Decision: DEC-SUPERVISOR-AUTORESTART-001 (MIGRATION.md entry #1052).
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from brutex.models import BruteXError

logger = logging.getLogger(__name__)


class _WaitSentinel(enum.Enum):
    """Distinguishes 'operator stop while the child was alive' from an exit code.

    A single-member enum used purely as a typed sentinel: the interruptible wait
    returns :data:`_STOPPED_WHILE_ALIVE` (not an ``int``) when it observed an
    operator stop with the child still running, so :meth:`ProcessSupervisor.run`
    can branch to the graceful-shutdown path. mypy narrows the enum literal
    cleanly, so the post-check ``exit_code`` is a plain ``int``.
    """

    STOPPED_WHILE_ALIVE = "stopped_while_alive"


#: Sentinel returned by the interruptible wait when an operator stop arrived
#: WHILE the child was still alive (so the child must be terminated, not reaped).
_STOPPED_WHILE_ALIVE: Final[Literal[_WaitSentinel.STOPPED_WHILE_ALIVE]] = (
    _WaitSentinel.STOPPED_WHILE_ALIVE
)


# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: Audit action stamped each time the supervisor relaunches the child.
AUDIT_ACTION_RESTART: Final[str] = "SUPERVISOR_RESTART"

#: Audit action stamped when the child exits cleanly and the supervisor stops.
AUDIT_ACTION_CLEAN_EXIT: Final[str] = "SUPERVISOR_CLEAN_EXIT"

#: Audit action stamped when an operator stop signal shuts the child down.
AUDIT_ACTION_OPERATOR_STOP: Final[str] = "SUPERVISOR_OPERATOR_STOP"

#: Audit action stamped when the restart cap is exhausted (loud give-up).
AUDIT_ACTION_GIVE_UP: Final[str] = "SUPERVISOR_GIVE_UP"

#: Default backoff base (seconds) for the first relaunch.
DEFAULT_BACKOFF_BASE_SECONDS: Final[float] = 1.0

#: Default multiplier applied to the backoff after each consecutive crash.
DEFAULT_BACKOFF_FACTOR: Final[float] = 2.0

#: Default ceiling (seconds) the exponential backoff can never exceed.
DEFAULT_BACKOFF_CAP_SECONDS: Final[float] = 300.0

#: Default stable-uptime window (seconds): a child that stayed up longer than
#: this before dying is NOT a crash loop, so its death resets the backoff.
DEFAULT_RESET_AFTER_UPTIME_SECONDS: Final[float] = 60.0

#: Default max consecutive unexpected exits before the supervisor gives up.
DEFAULT_MAX_RESTARTS: Final[int] = 10

#: Default grace period (seconds) the supervisor waits for a child to exit
#: after a SIGTERM before escalating to SIGKILL on an operator stop.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final[float] = 10.0

#: Default rolling window (seconds) over which abnormal-exit relaunches are
#: counted for the best-effort crash-loop alert
#: (DEC-SUPERVISOR-CRASHLOOP-ALERT-001): the count of relaunch timestamps
#: within this window is handed to the injected alerter, which escalates to
#: CRITICAL once the count reaches its crash-loop threshold.
DEFAULT_ALERT_WINDOW_SECONDS: Final[float] = 600.0

#: Default slice (seconds) for the INTERRUPTIBLE child wait: the supervisor polls
#: ``child.wait_for(poll)`` in slices this long, re-checking the operator-stop
#: predicate between slices so a SIGTERM arriving while the child is alive is
#: acted on within one slice (a bare blocking ``wait`` would not be interrupted —
#: PEP 475 auto-retries ``os.waitpid``). Small enough to be prompt, large enough
#: to keep the poll overhead negligible against a long-running child.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 0.5


# ────────────────────────────────────────────────────────────────────────
# Injected collaborators (Protocols) + defaults
# ────────────────────────────────────────────────────────────────────────


class ChildProcess(Protocol):
    """A running child process the supervisor monitors.

    Matched by :class:`PopenChildProcess` (the production ``subprocess.Popen``
    wrapper) and by any test fake exposing the same surface, so the supervisor
    never depends on the concrete process class.

    A value-returning stub method ``raise``\\s ``NotImplementedError`` (a real
    statement, never a bare ``...`` no-op flagged by static analysis, and it
    satisfies the declared return type); the ``None``-returning ones are a
    docstring only.
    """

    @property
    def pid(self) -> int:
        """The child's OS process ID (for logging / audit)."""
        raise NotImplementedError

    def wait_for(self, timeout: float) -> int | None:
        """Block up to *timeout* seconds; return the code, or ``None`` if still alive.

        On POSIX a NEGATIVE return code means the child was killed by signal
        ``-returncode`` (Python's :class:`subprocess.Popen` convention); the
        supervisor treats any non-zero code (positive OR negative) as an
        UNEXPECTED exit. This is the ONLY wait the supervisor uses — it polls in
        short slices so an operator stop is honoured promptly (a single blocking
        ``wait`` would not be interrupted by a delivered signal, PEP 475).
        """
        raise NotImplementedError

    def terminate(self) -> None:
        """Ask the child to exit gracefully (SIGTERM on POSIX)."""

    def kill(self) -> None:
        """Force-kill the child (SIGKILL on POSIX)."""


#: Launches a fresh child and returns its :class:`ChildProcess`. Injected so the
#: production path builds a real ``subprocess.Popen`` and tests build a fake.
ChildLauncher = Callable[[], ChildProcess]

#: Monotonic-seconds source. Default :func:`time.monotonic`; tests fake it.
Clock = Callable[[], float]

#: Sleeps for the given seconds. Default :func:`time.sleep`; tests fake it.
Sleeper = Callable[[float], None]

#: Returns ``True`` once an operator stop signal (SIGTERM/SIGINT) has arrived.
#: Injected so the loop is testable without installing real signal handlers;
#: :func:`install_signal_stop` wires a real one.
StopRequested = Callable[[], bool]


def _never_stop() -> bool:
    """Default :data:`StopRequested` — the operator never asks to stop."""
    return False


class RelaunchAlerter(Protocol):
    """Structural type for the operator-alert sink the relaunch + give-up branches fire.

    Matched by :class:`brutex.notifications.sweep_alerts.SweepAlerter` (whose
    :meth:`child_relaunched` decides the WARN-vs-CRITICAL severity from the
    in-window *relaunch_count*, and whose :meth:`supervisor_gave_up` is the
    terminal CRITICAL — the sweep is DOWN and nothing will relaunch it) and by
    any test fake exposing the same two-method surface — so the supervisor
    never imports the notifications package
    (DEC-SUPERVISOR-CRASHLOOP-ALERT-001). Best-effort by contract: the
    supervisor swallows + logs ANY exception the sink raises, so a buggy
    alerter can never crash or block the relaunch loop or the give-up verdict.
    """

    def child_relaunched(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        relaunch_count: int,
        detail: str,
    ) -> bool:
        """Emit one relaunch alert; return ``True`` iff a live notifier was attempted."""
        raise NotImplementedError

    def supervisor_gave_up(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        restarts: int,
        max_restarts: int,
    ) -> bool:
        """Emit the terminal give-up alert; return ``True`` iff a live notifier was attempted."""
        raise NotImplementedError


class AuditSink(Protocol):
    """Structural type for the hash-chained audit writer the supervisor stamps to.

    Matched by :class:`brutex.orchestrator.audit.AuditWriter` and
    :class:`brutex.engine.sweep_audit.SweepAuditWriter` (and by any test fake
    exposing the same one-method surface), so the supervisor never depends on a
    concrete writer class or its import chain.
    """

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
        """Append one hash-chained audit row and return the written entry."""


# ────────────────────────────────────────────────────────────────────────
# Exit classification + outcome
# ────────────────────────────────────────────────────────────────────────


class SupervisorOutcome(StrEnum):
    """Why the supervise loop stopped."""

    #: The child exited cleanly (code 0) — the sweep genuinely completed.
    CLEAN_EXIT = "clean_exit"
    #: An operator stop signal shut the child down; no relaunch.
    OPERATOR_STOP = "operator_stop"
    #: The restart cap was exhausted by a crash loop — loud give-up.
    GAVE_UP = "gave_up"


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """The terminal verdict of a :meth:`ProcessSupervisor.run`.

    Attributes
    ----------
    outcome
        Why the loop stopped.
    restarts
        How many times the child was relaunched over the whole run.
    last_exit_code
        The child's final ``returncode`` (``None`` if it was never observed to
        exit — only possible on an operator stop before the first ``wait``).
    """

    outcome: SupervisorOutcome
    restarts: int
    last_exit_code: int | None


# ────────────────────────────────────────────────────────────────────────
# Backoff policy
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential-backoff-with-cap policy for consecutive relaunches.

    The delay before the ``n``-th CONSECUTIVE relaunch (``n`` starting at 0) is
    ``min(base * factor**n, cap)``. A child that stayed up longer than
    ``reset_after_uptime_seconds`` resets ``n`` to 0 (it was not a crash loop).

    Parameters
    ----------
    base_seconds
        Delay before the first relaunch. Must be ``> 0``.
    factor
        Multiplier per consecutive crash. Must be ``>= 1`` (``1`` = a flat,
        non-growing backoff).
    cap_seconds
        Ceiling the delay can never exceed. Must be ``>= base_seconds``.
    reset_after_uptime_seconds
        A child up longer than this before dying resets the backoff. Must be
        ``> 0``.
    max_restarts
        Give up LOUDLY after this many consecutive unexpected exits. Must be
        ``>= 1``.
    """

    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    factor: float = DEFAULT_BACKOFF_FACTOR
    cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    reset_after_uptime_seconds: float = DEFAULT_RESET_AFTER_UPTIME_SECONDS
    max_restarts: int = DEFAULT_MAX_RESTARTS

    def __post_init__(self) -> None:
        if self.base_seconds <= 0:
            raise ValueError(f"base_seconds must be > 0; got {self.base_seconds!r}")
        if self.factor < 1:
            raise ValueError(f"factor must be >= 1; got {self.factor!r}")
        if self.cap_seconds < self.base_seconds:
            raise ValueError(
                f"cap_seconds ({self.cap_seconds!r}) must be >= base_seconds "
                f"({self.base_seconds!r})"
            )
        if self.reset_after_uptime_seconds <= 0:
            raise ValueError(
                f"reset_after_uptime_seconds must be > 0; got {self.reset_after_uptime_seconds!r}"
            )
        if self.max_restarts < 1:
            raise ValueError(f"max_restarts must be >= 1; got {self.max_restarts!r}")

    def delay_for(self, consecutive_index: int) -> float:
        """Return the backoff delay (seconds) before the ``consecutive_index``-th relaunch.

        ``consecutive_index`` starts at 0 for the first relaunch of a streak. The
        result is ``min(base * factor**index, cap)`` — always in
        ``[base_seconds, cap_seconds]``.
        """
        if consecutive_index < 0:
            raise ValueError(f"consecutive_index must be >= 0; got {consecutive_index!r}")
        raw = self.base_seconds * (self.factor**consecutive_index)
        return min(raw, self.cap_seconds)


# ────────────────────────────────────────────────────────────────────────
# ProcessSupervisor
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ProcessSupervisor:
    """Keep exactly one child alive until it succeeds or the operator stops it.

    Parameters
    ----------
    launch
        The injected :data:`ChildLauncher` — spawns a fresh child and returns
        its :class:`ChildProcess`. In production this builds a real
        ``subprocess.Popen`` (see :func:`build_subprocess_launcher`); tests
        inject a fake.
    policy
        The :class:`BackoffPolicy` (defaults are the module constants).
    subject
        The audit subject stamped on every supervisor row (e.g. the child
        command name). Defaults to ``"supervisor"``.
    audit
        Optional :class:`AuditSink`; when wired, every restart / clean-exit /
        operator-stop / give-up appends one hash-chained audit row.
    stop_requested
        Injected :data:`StopRequested` predicate (default: never). Consulted
        BETWEEN interruptible wait slices while the child is alive (so a stop
        gracefully shuts the child down), after every child exit, AND during the
        backoff wait so an operator stop is honoured promptly at every point.
    shutdown_grace_seconds
        Seconds the supervisor waits for the child to exit after SIGTERM before
        escalating to SIGKILL on an operator stop. Must be ``> 0``.
    poll_interval_seconds
        The slice (seconds) of the INTERRUPTIBLE child wait — the loop polls
        ``child.wait_for(poll_interval_seconds)`` and re-checks the stop
        predicate between slices. Must be ``> 0``.
    clock
        Monotonic-seconds source (default :func:`time.monotonic`) that times the
        child's uptime AND stamps the rolling relaunch-alert window — injected,
        so alert-escalation tests are fully deterministic (no real time reads).
    sleeper
        The injected :data:`Sleeper` used to wait out a backoff (default
        :func:`time.sleep`). Tests inject a fake and never sleep.
    alerter
        Optional :class:`RelaunchAlerter` (DEC-SUPERVISOR-CRASHLOOP-ALERT-001).
        When wired, EVERY abnormal-exit relaunch fires one best-effort
        ``child_relaunched`` alert carrying the count of relaunches within the
        rolling ``alert_window_seconds`` window — the alerter escalates to
        CRITICAL at its crash-loop threshold. ``None`` (the default) is a
        silent no-op and the relaunch behaviour is byte-unchanged.
    alert_window_seconds
        The rolling window (seconds) over which relaunch timestamps are
        counted for the alert escalation. Must be ``> 0``.

    Notes
    -----
    A buggy audit write OR a raising alerter is caught + logged and never
    crashes the supervisor (Golden Rule #10). All non-audit state is O(1) in
    the run length — a single restart counter, a consecutive-crash index, the
    last observed exit code, and a relaunch-timestamp window PRUNED to the
    rolling window on every relaunch (its size is bounded by the backoff-paced
    relaunch rate within one window, never by total restarts).
    """

    launch: ChildLauncher
    policy: BackoffPolicy = field(default_factory=BackoffPolicy)
    subject: str = "supervisor"
    audit: AuditSink | None = None
    stop_requested: StopRequested = _never_stop
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    clock: Clock = time.monotonic
    sleeper: Sleeper = time.sleep
    alerter: RelaunchAlerter | None = None
    alert_window_seconds: float = DEFAULT_ALERT_WINDOW_SECONDS
    _relaunch_times: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.shutdown_grace_seconds <= 0:
            raise ValueError(
                f"shutdown_grace_seconds must be > 0; got {self.shutdown_grace_seconds!r}"
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be > 0; got {self.poll_interval_seconds!r}"
            )
        if self.alert_window_seconds <= 0:
            raise ValueError(f"alert_window_seconds must be > 0; got {self.alert_window_seconds!r}")

    def run(self) -> SupervisorResult:
        """Supervise the child until it succeeds, is stopped, or the cap is hit.

        Returns
        -------
        SupervisorResult
            The terminal verdict — outcome, total restarts, and last exit code.
        """
        restarts = 0
        consecutive = 0
        last_exit_code: int | None = None

        # An operator stop that arrives BEFORE the first launch short-circuits:
        # never spawn a child we are only going to immediately kill.
        if self.stop_requested():
            self._stamp(AUDIT_ACTION_OPERATOR_STOP, {"phase": "pre_launch"})
            return SupervisorResult(SupervisorOutcome.OPERATOR_STOP, restarts, last_exit_code)

        while True:
            child = self.launch()
            start = self.clock()
            logger.info("supervisor: child launched (pid=%s)", child.pid)
            exit_code = self._wait_for_exit_or_stop(child)
            uptime = self.clock() - start

            # An operator stop arrived WHILE the child was still alive: shut the
            # child down gracefully (SIGTERM → grace wait → SIGKILL) and do NOT
            # relaunch. This is the headline path — the operator ran `launchctl
            # stop` / `kill`, so the running child must actually be terminated,
            # not orphaned. ``_wait_for_exit_or_stop`` returns the sentinel when it
            # observed the stop with the child still alive; the shutdown happens
            # HERE via ``shutdown_child``.
            if exit_code is _STOPPED_WHILE_ALIVE:
                shutdown_code = self.shutdown_child(child)
                last_exit_code = shutdown_code if shutdown_code is not None else last_exit_code
                self._stamp(
                    AUDIT_ACTION_OPERATOR_STOP,
                    {
                        "phase": "shutdown",
                        "exit_code": shutdown_code,
                        "uptime_seconds": uptime,
                    },
                )
                logger.info("supervisor: operator stop — child shut down, not relaunching")
                return SupervisorResult(SupervisorOutcome.OPERATOR_STOP, restarts, last_exit_code)

            last_exit_code = exit_code

            # An operator stop that raced the child's own exit: honour the stop,
            # do not relaunch (the child is already gone, so no shutdown needed).
            if self.stop_requested():
                self._stamp(
                    AUDIT_ACTION_OPERATOR_STOP,
                    {"phase": "post_exit", "exit_code": exit_code, "uptime_seconds": uptime},
                )
                return SupervisorResult(SupervisorOutcome.OPERATOR_STOP, restarts, last_exit_code)

            if exit_code == 0:
                self._stamp(
                    AUDIT_ACTION_CLEAN_EXIT,
                    {"uptime_seconds": uptime, "restarts": restarts},
                )
                logger.info("supervisor: child exited cleanly — stopping")
                return SupervisorResult(SupervisorOutcome.CLEAN_EXIT, restarts, last_exit_code)

            # Unexpected exit → relaunch policy. A long-lived child that finally
            # died is not a crash loop, so reset the consecutive-crash streak.
            if uptime >= self.policy.reset_after_uptime_seconds:
                consecutive = 0

            if consecutive >= self.policy.max_restarts:
                self._stamp(
                    AUDIT_ACTION_GIVE_UP,
                    {
                        "exit_code": exit_code,
                        "restarts": restarts,
                        "max_restarts": self.policy.max_restarts,
                    },
                )
                logger.error(
                    "supervisor: giving up after %d consecutive restarts (last exit=%d)",
                    restarts,
                    exit_code,
                )
                # Terminal give-up CRITICAL (DEC-SUPERVISOR-CRASHLOOP-ALERT-001,
                # hostile-review MINOR-3 fold): the sweep is DOWN and nothing
                # will relaunch it — the single most alert-worthy state, so it
                # must never rely on the in-window relaunch count having reached
                # the crash-loop bar first (a max_restarts < threshold policy or
                # a slow, backoff-spaced loop would otherwise give up silently).
                self._alert_gave_up(exit_code=exit_code, restarts=restarts)
                return SupervisorResult(SupervisorOutcome.GAVE_UP, restarts, last_exit_code)

            delay = self.policy.delay_for(consecutive)
            restarts += 1
            consecutive += 1
            self._stamp(
                AUDIT_ACTION_RESTART,
                {
                    "exit_code": exit_code,
                    "uptime_seconds": uptime,
                    "backoff_seconds": delay,
                    "restart_index": restarts,
                    "consecutive": consecutive,
                },
            )
            logger.warning(
                "supervisor: child exited unexpectedly (code=%d, up %.1fs); relaunch #%d in %.1fs",
                exit_code,
                uptime,
                restarts,
                delay,
            )
            # Best-effort operator alert (DEC-SUPERVISOR-CRASHLOOP-ALERT-001):
            # a silently-looping abnormal exit (e.g. an OOM SIGKILL every few
            # minutes) must reach the operator. Fired AFTER the audit stamp and
            # BEFORE the backoff sleep; any alerter failure is swallowed +
            # logged, so relaunch behaviour and exit state stay byte-unchanged.
            self._alert_relaunch(exit_code=exit_code, uptime=uptime, delay=delay, restarts=restarts)
            if self._sleep_backoff(delay):
                # An operator stop arrived during the backoff wait — do not
                # relaunch; nothing to shut down (the child already exited).
                self._stamp(
                    AUDIT_ACTION_OPERATOR_STOP,
                    {"phase": "during_backoff", "restarts": restarts},
                )
                return SupervisorResult(SupervisorOutcome.OPERATOR_STOP, restarts, last_exit_code)

    def _wait_for_exit_or_stop(
        self, child: ChildProcess
    ) -> int | Literal[_WaitSentinel.STOPPED_WHILE_ALIVE]:
        """INTERRUPTIBLY wait for *child* to exit, honouring an operator stop.

        Polls ``child.wait_for(poll_interval_seconds)`` in short slices, checking
        the operator-stop predicate BEFORE each slice. Returns the child's exit
        code once it exits, or :data:`_STOPPED_WHILE_ALIVE` the moment a stop is
        observed while the child is still running — the caller then shuts the
        child down gracefully.

        A single blocking ``wait`` cannot be used here: a bare ``os.waitpid`` is
        NOT interrupted by a delivered signal (PEP 475 auto-retries it), so it
        would block until the child died on its own and the operator stop would
        never be acted on — the exact orphaned-child bug this poll loop fixes.
        """
        while True:
            if self.stop_requested():
                return _STOPPED_WHILE_ALIVE
            code = child.wait_for(self.poll_interval_seconds)
            if code is not None:
                return code

    def _sleep_backoff(self, delay: float) -> bool:
        """Wait out *delay* seconds, returning ``True`` if an operator stop arrived.

        Sleeps in short slices (``poll_interval_seconds`` each) via the injected
        sleeper, re-checking the stop predicate BETWEEN slices, so an operator
        stop that arrives DURING a long backoff is honoured within one slice
        rather than at the end of the whole delay. (A single ``time.sleep`` is
        NOT interrupted by a delivered signal — PEP 475 auto-retries it — so
        slicing is what makes the stop prompt.) An injected sleeper in tests
        records every slice and stays deterministic.
        """
        remaining = delay
        while remaining > 0:
            if self.stop_requested():
                return True
            slice_seconds = min(self.poll_interval_seconds, remaining)
            self.sleeper(slice_seconds)
            remaining -= slice_seconds
        return self.stop_requested()

    def shutdown_child(self, child: ChildProcess) -> int | None:
        """Gracefully stop *child* (SIGTERM, wait, then SIGKILL). Return its code.

        Public so an owner that holds the handle (a signal handler wired outside
        the loop) can shut the child down on an operator stop. Returns the
        child's exit code, or ``None`` if it never exited even after SIGKILL
        (only possible on a truly wedged/unkillable process).
        """
        child.terminate()
        code = child.wait_for(self.shutdown_grace_seconds)
        if code is not None:
            return code
        logger.warning(
            "supervisor: child (pid=%s) did not exit within %.1fs of SIGTERM — killing",
            child.pid,
            self.shutdown_grace_seconds,
        )
        child.kill()
        return child.wait_for(self.shutdown_grace_seconds)

    def _alert_relaunch(
        self, *, exit_code: int, uptime: float, delay: float, restarts: int
    ) -> None:
        """Fire one best-effort relaunch alert with the rolling in-window count.

        Records this relaunch's timestamp on the injected :attr:`clock`, prunes
        the window to ``alert_window_seconds``, and hands the in-window count to
        the injected :class:`RelaunchAlerter` — WHICH owns the WARN-vs-CRITICAL
        crash-loop escalation decision (3+ in-window relaunches per
        :data:`brutex.notifications.sweep_alerts.CRASH_LOOP_RELAUNCH_THRESHOLD`).
        A POSIX death-by-signal (negative ``returncode``) is surfaced as the
        killing signal number. ANY exception from the alerter is swallowed +
        logged — an alert can never crash, block, or slow the relaunch loop
        (Golden Rule #10); with no alerter wired this is a silent no-op that
        keeps the loop byte-unchanged.
        """
        if self.alerter is None:
            return
        now = self.clock()
        self._relaunch_times.append(now)
        cutoff = now - self.alert_window_seconds
        self._relaunch_times[:] = [t for t in self._relaunch_times if t > cutoff]
        detail = f"uptime {uptime:.1f}s, backoff {delay:.1f}s, restart #{restarts} this run"
        try:
            self.alerter.child_relaunched(
                child=self.subject,
                exit_code=exit_code,
                signal=-exit_code if exit_code < 0 else None,
                relaunch_count=len(self._relaunch_times),
                detail=detail,
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert seam
            logger.exception(
                "supervisor: relaunch alert failed; the relaunch itself proceeds unaffected"
            )

    def _alert_gave_up(self, *, exit_code: int, restarts: int) -> None:
        """Fire the terminal give-up alert, best-effort (never changes the verdict).

        Hands the give-up facts to the injected :class:`RelaunchAlerter`'s
        :meth:`~RelaunchAlerter.supervisor_gave_up` — WHICH owns the CRITICAL
        severity (the sweep is DOWN; no further relaunches). A POSIX
        death-by-signal (negative ``returncode``) is surfaced as the killing
        signal number, mirroring :meth:`_alert_relaunch`. ANY exception from
        the alerter is swallowed + logged — the give-up verdict, audit stamp,
        and exit state stay byte-unchanged (Golden Rule #10); with no alerter
        wired this is a silent no-op.
        """
        if self.alerter is None:
            return
        try:
            self.alerter.supervisor_gave_up(
                child=self.subject,
                exit_code=exit_code,
                signal=-exit_code if exit_code < 0 else None,
                restarts=restarts,
                max_restarts=self.policy.max_restarts,
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert seam
            logger.exception(
                "supervisor: give-up alert failed; the give-up verdict stands unaffected"
            )

    def _stamp(self, action: str, payload: dict[str, Any]) -> None:
        """Append one hash-chained audit row, best-effort (never crashes the run)."""
        if self.audit is None:
            return
        try:
            self.audit.append(action=action, subject=self.subject, payload=payload)
        except (OSError, BruteXError):
            logger.exception(
                "supervisor: audit append failed for action=%s; state still recorded, "
                "supervisor continues",
                action,
            )


# ────────────────────────────────────────────────────────────────────────
# Production adapters — real subprocess + real signal handlers
# ────────────────────────────────────────────────────────────────────────
#
# These wrap the OS surfaces the supervisor abstracts over. The ``PopenChildProcess``
# process methods + ``build_subprocess_launcher`` + ``install_signal_stop`` are
# ``# pragma: no cover`` because they spawn a REAL child / install REAL signal
# handlers — the same live-side-effect boundary the CLI daemon loop
# (``cmd_sweep_daemon``) is excluded at. The PURE loop above is 100%-tested with a
# fake ``ChildProcess`` + injected clock/sleeper; a trivial end-to-end integration
# test spawns ONE deterministic real subprocess to prove the wrapper wiring. The
# ``_SignalStop`` LATCH is a pure in-process flag (no OS side effect) and IS
# unit-tested — only its real-signal INSTALLATION carries a pragma.


class PopenChildProcess:  # pragma: no cover — thin wrapper over a real subprocess.Popen
    """A :class:`ChildProcess` backed by a real :class:`subprocess.Popen`.

    Wraps the live process surface the supervisor needs: ``wait_for`` (bounded
    poll), ``terminate`` (SIGTERM), and ``kill`` (SIGKILL). Python's
    ``Popen.returncode`` is NEGATIVE when the child died by signal, which the
    supervisor already classifies as an unexpected exit.
    """

    __slots__ = ("_proc",)

    def __init__(self, proc: Any) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        """The child's OS process ID."""
        return int(self._proc.pid)

    def wait_for(self, timeout: float) -> int | None:
        """Block up to *timeout* seconds; return the code, or ``None`` if alive."""
        import subprocess  # nosec B404 — stdlib subprocess, list-argv launch, no shell (see below)

        try:
            return int(self._proc.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> None:
        """Send SIGTERM to the child."""
        self._proc.terminate()

    def kill(self) -> None:
        """Force-kill the child (SIGKILL on POSIX)."""
        self._proc.kill()


def build_subprocess_launcher(
    argv: list[str],
) -> ChildLauncher:  # pragma: no cover — spawns a real OS process
    """Return a :data:`ChildLauncher` that spawns ``argv`` as a fresh subprocess.

    Each call to the returned launcher starts a brand-new ``subprocess.Popen``
    of *argv* and wraps it in a :class:`PopenChildProcess`. The child inherits the
    parent's stdio + environment (so the sweep daemon logs to the same console
    and reads the same ``BRUTEX_*`` config); no shell is used (``argv`` is a
    list, never a string) — no shell-injection surface. It is started in its OWN
    session/process group (``start_new_session=True``) so a graceful shutdown can
    signal it cleanly and an operator's terminal Ctrl-C is not forwarded to it
    twice (the supervisor's SIGINT handler is the single stop authority).
    """
    import subprocess  # nosec B404 — stdlib subprocess, list-argv launch, no shell (see B603 below)

    def _launch() -> ChildProcess:
        # nosec B603: `argv` is a fixed list built from `sys.executable` + literal
        # subcommand tokens (see cli._supervisor_child_argv) — NOT a shell string
        # and NOT untrusted input; `shell=` is never set, so there is no
        # shell-injection surface. `start_new_session=True` isolates the child's
        # process group for clean signalling.
        proc = subprocess.Popen(  # nosec B603  # noqa: S603 — list argv, no shell, trusted CLI
            argv, start_new_session=True
        )
        return PopenChildProcess(proc)

    return _launch


class _SignalStop:
    """A latching stop flag flipped by a delivered SIGTERM/SIGINT.

    Once set it stays set (a second signal is a no-op), so the supervisor's
    :data:`StopRequested` predicate reads ``True`` for the rest of the run. The
    latch logic is a pure in-process flag with NO OS side effect, so it is
    directly unit-testable (and IS unit-tested); only :func:`install_signal_stop`
    (which installs REAL OS handlers) carries a coverage pragma.
    """

    __slots__ = ("_stopped",)

    def __init__(self) -> None:
        self._stopped = False

    def __call__(self) -> bool:
        """Return ``True`` once a stop signal has been delivered."""
        return self._stopped

    def trip(self, _signum: int, _frame: Any) -> None:
        """Signal handler — latch the stop flag (idempotent; a repeat is a no-op)."""
        self._stopped = True


def install_signal_stop() -> StopRequested:  # pragma: no cover — installs real OS handlers
    """Install SIGTERM/SIGINT handlers and return a :data:`StopRequested` predicate.

    Wires a latching :class:`_SignalStop` to both SIGTERM (the ``systemd`` /
    ``launchd`` stop signal) and SIGINT (Ctrl-C) so an operator stop flips the
    supervisor's stop predicate; the supervisor then shuts the child down
    gracefully and does NOT relaunch. Returns the predicate to inject as
    ``ProcessSupervisor(stop_requested=...)``.
    """
    import signal

    stop = _SignalStop()
    signal.signal(signal.SIGTERM, stop.trip)
    signal.signal(signal.SIGINT, stop.trip)
    return stop


__all__ = [
    "AUDIT_ACTION_CLEAN_EXIT",
    "AUDIT_ACTION_GIVE_UP",
    "AUDIT_ACTION_OPERATOR_STOP",
    "AUDIT_ACTION_RESTART",
    "DEFAULT_ALERT_WINDOW_SECONDS",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_MAX_RESTARTS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_RESET_AFTER_UPTIME_SECONDS",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "AuditSink",
    "BackoffPolicy",
    "ChildLauncher",
    "ChildProcess",
    "Clock",
    "PopenChildProcess",
    "ProcessSupervisor",
    "RelaunchAlerter",
    "Sleeper",
    "StopRequested",
    "SupervisorOutcome",
    "SupervisorResult",
    "build_subprocess_launcher",
    "install_signal_stop",
]

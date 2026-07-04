"""Hung / stalled dashboard-pull detector (DEC-PULL-STALL-DETECTION-001).

The dashboard background pull (:class:`brutex.dashboard.pull_jobs.BackgroundPullJob`)
runs the pull *work* off the request thread and clears its single-flight ``_running``
flag ONLY when the worker returns or raises. A worker that HANGS — e.g. wedged on the
no-timeout partition ``flock`` (``brutex/data/partition_lock.py``), a deadlocked socket,
an infinite vendor retry — never reaches that ``finally``, so ``_running`` stays ``True``
forever and the single-flight guard then SILENTLY refuses every future pull. The proven
:class:`brutex.observability.watchdog.Watchdog` heartbeat primitive existed but was wired
ONLY into the sweep engine, never the dashboard pull — so a hung pull was undetectable
(the failure hunt's GAP 1, MAJOR).

This module closes that gap by mirroring the sweep-engine watchdog wiring
(:func:`brutex.engine.sweep_supervisor._drive_watchdog`) at the dashboard-pull level:

* :class:`PullWatchdog` owns the :class:`~brutex.observability.watchdog.Watchdog`
  hysteresis state machine (HEALTHY↔HUNG, monotonic clock, edge-only callbacks) for ONE
  pull worker. The pull worker calls :meth:`PullWatchdog.beat` on **real progress** (per
  fetched window), and a background timer thread calls :meth:`PullWatchdog.check_once` on
  a cadence so a hung worker — which cannot poll itself — is still detected.
* On a HEALTHY→HUNG edge the watchdog fires the operator-supplied ``on_hung`` callback.
  The dashboard wires a CRITICAL Telegram alert + marks the job STALLED-SUSPECT
  (DEC-PIPELINE-STALL-HEARTBEAT-001 — an honest DEGRADED observation; it does NOT fail
  the run or release the single-flight while the worker thread is still alive, closing
  the forensically-proven false-positive kill + double-run race). A later heartbeat
  fires the HUNG→HEALTHY :attr:`PullWatchdog.on_recovery` hook, clearing the marker.

## Why a separate timer thread

The pull worker thread is exactly the thread that hangs, so it cannot detect its own
stall. A daemon timer thread ticking :meth:`check_once` is the only place a hung worker
can be observed — identical to the sweep supervisor's ``sweep-watchdog`` daemon.

## Honest envelope (Golden Rule #10)

This DETECTS + alerts a stall (the operator's real-time guarantee) — it does NOT itself
PREVENT the underlying hang (the no-timeout ``flock`` is a separate MINOR follow-up). The
timeout is an operator-tunable heuristic (constant / env), not a vendor limit; a breach
is an OBSERVATION + alert, never auto-remediation of the wedged I/O. Every callback is
best-effort: a raising ``on_hung`` is swallowed + logged by the underlying watchdog and
can never crash the timer or the pull. ``beat`` / ``check_once`` are O(1) (one worker).

## Decision: DEC-PULL-STALL-DETECTION-001 (MIGRATION.md entry #945).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Final

from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState

logger = logging.getLogger(__name__)

#: The single worker name the dashboard pull beats under. One job runs at a time
#: (single-flight), so one worker name is enough.
PULL_WORKER_NAME: Final[str] = "dashboard-pull"

#: Default stall timeout (seconds): a dashboard pull with no fetched-window heartbeat for
#: this long is declared HUNG. Generous — a healthy pull beats every settled window
#: (~1 s cadence under load), so only a true wedge (deadlocked flock / socket) trips it,
#: never a slow-but-progressing backfill. Operator-tunable via :data:`PULL_STALL_ENV`.
DEFAULT_STALL_TIMEOUT_SECONDS: Final[float] = 600.0

#: Environment override for the stall timeout (operator escape hatch, no code change).
PULL_STALL_ENV: Final[str] = "BRUTEX_PULL_STALL_TIMEOUT_SECONDS"

#: Default wedge multiplier (DEC-PULL-WEDGE-ESCALATE-001): a HUNG worker whose silence
#: reaches ``wedge_multiplier × stall timeout`` (default 6 × 600 s = 1 hour) with the DUAL
#: signal confirmed (zero heartbeats AND no lake-revision movement since the HUNG edge)
#: escalates to the terminal WEDGED verdict. Operator-tunable via :data:`PULL_WEDGE_ENV`.
DEFAULT_WEDGE_MULTIPLIER: Final[float] = 6.0

#: Environment override for the wedge multiplier (operator escape hatch, no code change).
PULL_WEDGE_ENV: Final[str] = "BRUTEX_PULL_WEDGE_MULTIPLIER"


def resolve_stall_timeout_seconds() -> float:
    """Resolve the pull stall timeout: env override, else the audited default.

    Reads :data:`PULL_STALL_ENV` (:data:`BRUTEX_PULL_STALL_TIMEOUT_SECONDS`); falls back to
    :data:`DEFAULT_STALL_TIMEOUT_SECONDS` when unset, blank, non-numeric, or non-positive —
    a bad value can never crash a pull or silently disable the detector (fail-safe to the
    audited default, mirroring :func:`brutex.adapters.groww.constants.resolve_pool_size`).
    O(1).
    """
    raw = os.environ.get(PULL_STALL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALL_TIMEOUT_SECONDS
    if value <= 0.0:
        return DEFAULT_STALL_TIMEOUT_SECONDS
    return value


def resolve_wedge_multiplier() -> float:
    """Resolve the wedge multiplier: env override, else the audited default.

    Reads :data:`PULL_WEDGE_ENV` (``BRUTEX_PULL_WEDGE_MULTIPLIER``); falls back to
    :data:`DEFAULT_WEDGE_MULTIPLIER` when unset, blank, non-numeric, or non-positive —
    a bad value can never crash a pull or make the escalation fire before the HUNG edge
    (fail-safe to the audited default, mirroring :func:`resolve_stall_timeout_seconds`).
    O(1).
    """
    raw = os.environ.get(PULL_WEDGE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WEDGE_MULTIPLIER
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WEDGE_MULTIPLIER
    if value <= 0.0:
        return DEFAULT_WEDGE_MULTIPLIER
    return value


#: The HEALTHY→HUNG edge callback (one worker → one event). Best-effort: a raising
#: callback is swallowed + logged by the underlying :class:`Watchdog`.
OnHung = Callable[[WorkerEvent], None]

#: Background timer spawner: runs *fn* (the poll loop) off the caller's thread. The default
#: spawns a daemon thread; tests inject a synchronous / capturing runner for determinism.
TimerSpawn = Callable[[Callable[[], None]], None]


def _daemon_spawn(fn: Callable[[], None]) -> None:  # pragma: no cover — real-thread default
    """Run *fn* in a fire-and-forget daemon thread (the live default)."""
    threading.Thread(target=fn, name="dashboard-pull-watchdog", daemon=True).start()


class PullWatchdog:
    """Heartbeat watchdog for ONE dashboard pull worker — a thin, value-adding wrapper.

    Mirrors :class:`brutex.engine.sweep_watchdog.SweepWatchdog`: the underlying
    :class:`~brutex.observability.watchdog.Watchdog` owns the HEALTHY↔HUNG hysteresis +
    monotonic clock; this wrapper adds (a) pull-appropriate defaults, (b) the background
    poll loop + ``stop`` lifecycle so a hung worker (which blocks its own thread) is still
    detected, and (c) thread-safety so the worker thread can ``beat`` while the timer
    thread ``check_once``s.

    Parameters
    ----------
    on_hung
        Edge callback fired once per HEALTHY→HUNG transition. The dashboard wires a
        CRITICAL Telegram alert + the job's stalled-transition. Best-effort — a raising
        callback is swallowed by the underlying watchdog.
    timeout_seconds
        Stall timeout (defaults to :func:`resolve_stall_timeout_seconds`). Must be ``> 0``.
    spawn
        Background timer spawner (defaults to a daemon thread). Inject a capturing runner
        in tests for determinism.
    clock
        Monotonic-seconds source forwarded to the underlying :class:`Watchdog` (defaults
        to :func:`time.monotonic`); injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        on_hung: OnHung,
        timeout_seconds: float | None = None,
        spawn: TimerSpawn = _daemon_spawn,
        clock: Callable[[], float] | None = None,
        wedge_multiplier: float | None = None,
    ) -> None:
        self._timeout = (
            resolve_stall_timeout_seconds() if timeout_seconds is None else timeout_seconds
        )
        self._spawn = spawn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: Wedged-worker escalation (DEC-PULL-WEDGE-ESCALATE-001, STAGE 1). Fired AT MOST
        #: ONCE per run (a terminal latch) when the DUAL signal holds at
        #: ``wedge_multiplier × timeout`` of silence: zero heartbeats since the HUNG edge
        #: AND the injected ``revision_probe`` token unchanged vs the HUNG-edge baseline.
        #: Settable AFTER construction (mirrors ``on_recovery``) so the injected
        #: ``watchdog_factory`` signature stays ``(on_hung) -> PullWatchdog``. Best-effort
        #: — a raising callback is swallowed + logged, never crashing the timer.
        self.on_wedged: OnHung | None = None
        #: Lake-revision probe for the DUAL signal's second half (``None`` = no signal →
        #: the escalation ABSTAINS entirely; a probe-less deployment never escalates —
        #: the honest envelope, stated not faked). Settable after construction.
        self.revision_probe: Callable[[], str | None] | None = None
        self._wedge_multiplier = (
            resolve_wedge_multiplier() if wedge_multiplier is None else wedge_multiplier
        )
        self._clock_fn: Callable[[], float] = clock if clock is not None else time.monotonic
        self._last_beat_at: float | None = None
        self._wedged_fired = False
        self._hung_baseline_token: str | None = None
        self._hung_baseline_captured = False
        #: When (if ever) the escalation OBSERVED the revision token move after the HUNG
        #: edge — the wedge window then restarts from that movement, so BOTH halves of
        #: the DUAL signal cover the full ``wedge_multiplier x timeout`` window.
        self._movement_observed_at: float | None = None
        #: Optional HUNG→HEALTHY edge callback (DEC-PIPELINE-STALL-HEARTBEAT-001): the
        #: dashboard job clears its stalled-suspect marker here when a heartbeat lands
        #: again after a HUNG declaration (e.g. the greeks/prebuild/movers seams resume
        #: beating). Settable AFTER construction so the injected ``watchdog_factory``
        #: signature stays ``(on_hung) -> PullWatchdog`` (back-compat). Best-effort —
        #: a raising callback is swallowed + logged, never crashing the timer.
        self.on_recovery: OnHung | None = None
        self._user_on_hung = on_hung
        wd_kwargs: dict[str, object] = {
            "timeout_seconds": self._timeout,
            # The HUNG edge first captures the wedge-escalation revision baseline
            # (DEC-PULL-WEDGE-ESCALATE-001), then forwards to the caller's hook.
            "on_hung": self._hung,
            # A recovery edge logs, then forwards to the optional ``on_recovery`` hook
            # (the dashboard clears its stalled-suspect marker there —
            # DEC-PIPELINE-STALL-HEARTBEAT-001).
            "on_recovery": self._recovery,
        }
        if clock is not None:
            wd_kwargs["clock"] = clock
        self._wd = Watchdog(**wd_kwargs)  # type: ignore[arg-type]

    @property
    def timeout_seconds(self) -> float:
        """The configured stall timeout (seconds)."""
        return self._timeout

    @property
    def wedge_multiplier(self) -> float:
        """The configured wedge multiplier (DEC-PULL-WEDGE-ESCALATE-001)."""
        return self._wedge_multiplier

    def _probe_token(self) -> str | None:
        """Best-effort read of the injected revision probe — ``None`` on any error.

        A probe that raises (a stalled external volume mid-``os.stat``) must never
        crash the timer loop; ``None`` is the probe's own "no signal" contract, so an
        erroring probe simply ABSTAINS from the escalation (never a false WEDGED).
        """
        probe = self.revision_probe
        if probe is None:
            return None
        try:
            return probe()
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            logger.exception("pull_stall.revision_probe failed; escalation abstains")
            return None

    def _hung(self, event: WorkerEvent) -> None:
        """HEALTHY→HUNG edge: capture the wedge baseline, then forward to the caller.

        The lake-revision token observed AT the HUNG edge is the baseline the
        escalation compares against (DEC-PULL-WEDGE-ESCALATE-001): "no movement since
        the HUNG edge". Runs inside :meth:`check_once`'s lock (the underlying watchdog
        fires callbacks synchronously from ``check_once``).
        """
        self._hung_baseline_token = self._probe_token()
        self._hung_baseline_captured = True
        self._movement_observed_at = None
        self._user_on_hung(event)

    def _maybe_escalate_locked(self) -> None:
        """Fire ``on_wedged`` ONCE when the DUAL wedge signal holds — caller holds the lock.

        DEC-PULL-WEDGE-ESCALATE-001, STAGE 1. Escalates ONLY when ALL hold:

        * the worker is currently HUNG and the escalation has not already fired (the
          terminal latch — a later heartbeat never un-latches a fired escalation);
        * ZERO heartbeats for ``wedge_multiplier × timeout`` (the last-beat age — a
          beat between polls resets it, so any sign of life defers the verdict);
        * the DUAL second half CONFIRMS: a revision probe is wired, a baseline was
          captured at the HUNG edge, BOTH tokens are real (an absent/erroring probe
          ABSTAINS — never escalate on a blind probe), and the token has NOT moved.
          A token that DID move re-baselines (the worker is writing — measure any
          later freeze against the newest observation).

        O(1) per poll. The hook is best-effort (swallow + log), mirroring
        :meth:`_recovery`.
        """
        if self._wedged_fired or self.on_wedged is None:
            return
        if self._wd.state_of(PULL_WORKER_NAME) is not WorkerState.HUNG:
            return
        if self._last_beat_at is None:  # pragma: no cover — start() always seeds it
            return
        age = self._clock_fn() - self._last_beat_at
        if age < self._wedge_multiplier * self._timeout:
            return
        if not self._hung_baseline_captured:
            return  # no HUNG-edge baseline (defensive) — abstain
        current = self._probe_token()
        if current is None or self._hung_baseline_token is None:
            # No probe / probe abstained on either side: the DUAL signal cannot be
            # confirmed — never escalate on heartbeat silence alone (honest envelope:
            # a probe-less deployment never reaches WEDGED).
            return
        if current != self._hung_baseline_token:
            # The lake moved — the worker IS making durable progress despite the silent
            # heartbeat. Re-baseline so a LATER freeze is measured from this observation,
            # and restart the wedge window from the movement (both DUAL halves must cover
            # the FULL window before a verdict).
            self._hung_baseline_token = current
            self._movement_observed_at = self._clock_fn()
            return
        if self._movement_observed_at is not None and (
            self._clock_fn() - self._movement_observed_at < self._wedge_multiplier * self._timeout
        ):
            # Movement WAS observed after the HUNG edge and the freeze since then has not
            # yet spanned the full wedge window — defer the verdict.
            return
        self._wedged_fired = True
        event = WorkerEvent(
            worker_name=PULL_WORKER_NAME,
            new_state=WorkerState.HUNG,
            last_beat_seconds_ago=age,
            timeout_seconds=self._timeout,
        )
        logger.critical(
            "pull_stall.wedged worker=%s last_beat_seconds_ago=%.1f wedge_multiplier=%.1f",
            event.worker_name,
            age,
            self._wedge_multiplier,
        )
        hook = self.on_wedged
        try:
            hook(event)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            logger.exception("pull_stall.on_wedged failed; the timer is unaffected")

    def _recovery(self, event: WorkerEvent) -> None:
        """Log a HUNG→HEALTHY edge, then forward it to the optional hook.

        The forward is best-effort (DEC-PIPELINE-STALL-HEARTBEAT-001): a raising
        ``on_recovery`` is swallowed + logged here — the underlying watchdog's own
        callback guard is narrower (``OSError/ValueError/RuntimeError``), so this
        wrapper must catch broadly or a buggy hook could kill the timer loop.
        """
        logger.info(
            "pull_stall.recovery worker=%s last_beat_seconds_ago=%.1f",
            event.worker_name,
            event.last_beat_seconds_ago,
        )
        hook = self.on_recovery
        if hook is None:
            return
        try:
            hook(event)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            logger.exception("pull_stall.on_recovery failed; the timer is unaffected")

    def start(self, *, poll_seconds: float | None = None) -> None:
        """Register the worker HEALTHY + launch the background poll loop.

        *poll_seconds* defaults to ``timeout / 3`` (the sweep-watchdog cadence) so a stall
        is detected within ~one extra poll of the timeout. The worker is registered up
        front so the very first :meth:`check_once` after the timeout can fire even if no
        ``beat`` ever landed (a worker that hangs before its first window).
        """
        with self._lock:
            self._wd.register(PULL_WORKER_NAME)
            # Registration counts as the initial heartbeat for the wedge-age clock
            # (mirrors Watchdog.register's own last-beat seed).
            self._last_beat_at = self._clock_fn()
        interval = self._timeout / 3.0 if poll_seconds is None else poll_seconds
        self._stop.clear()
        self._spawn(lambda: self._drive(interval))

    def _drive(self, interval: float) -> None:
        """Timer-loop body: :meth:`check_once` every *interval* s until stopped.

        Exits promptly when :meth:`stop` sets the event (``Event.wait`` returns ``True``);
        otherwise (a timeout → ``False``) it polls once and loops. Mirrors
        :func:`brutex.engine.sweep_supervisor._drive_watchdog`.
        """
        while not self._stop.wait(interval):
            self.check_once()

    def beat(self) -> None:
        """Record a heartbeat (real progress — a fetched window) — O(1), thread-safe."""
        with self._lock:
            self._wd.beat(PULL_WORKER_NAME)
            self._last_beat_at = self._clock_fn()

    def check_once(self) -> tuple[WorkerEvent, ...]:
        """Poll the heartbeat, firing the hung edge if the worker wedged — O(1)."""
        with self._lock:
            events = self._wd.check_once()
            # Wedged-worker escalation (DEC-PULL-WEDGE-ESCALATE-001): evaluated on the
            # SAME poll cadence, after the HUNG/recovery edges have fired.
            self._maybe_escalate_locked()
            return events

    def stop(self) -> None:
        """Stop the background poll loop (called from the worker's ``finally``)."""
        self._stop.set()

    def state(self) -> WorkerState:
        """Return the worker's current state (HEALTHY / HUNG) — test accessor."""
        with self._lock:
            return self._wd.state_of(PULL_WORKER_NAME)


__all__ = [
    "DEFAULT_STALL_TIMEOUT_SECONDS",
    "DEFAULT_WEDGE_MULTIPLIER",
    "PULL_STALL_ENV",
    "PULL_WEDGE_ENV",
    "PULL_WORKER_NAME",
    "OnHung",
    "PullWatchdog",
    "TimerSpawn",
    "resolve_stall_timeout_seconds",
    "resolve_wedge_multiplier",
]

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

import structlog

from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState

logger = logging.getLogger(__name__)

#: Structured channel for the throttled alive-beat (DEC-PULL-TELEMETRY-TRIAD-001) — the
#: happy path's ONE periodic proof-of-life line (the stall events above stay on the
#: pre-existing stdlib logger, byte-unchanged).
_LOG = structlog.get_logger("brutex.dashboard.pull_stall")

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

#: Minimum seconds between two ``pull_stall.watchdog_alive`` INFO lines — the ≤ 18
#: lines/hour bound (``3600 / 18 = 200``), DERIVED from the existing default check
#: cadence: the poll interval is ``timeout / 3`` and the audited default timeout is
#: :data:`DEFAULT_STALL_TIMEOUT_SECONDS` (600 s) → one check every 200 s = exactly 18
#: checks/hour, so at the default ONE line per check tick already satisfies the bound.
#: A SHRUNK env timeout (:data:`PULL_STALL_ENV`) raises the check frequency, so the
#: alive log throttles against ``max(check_interval, this floor)`` — the bound holds
#: for EVERY configuration, with no new thread (DEC-PULL-TELEMETRY-TRIAD-001).
ALIVE_LOG_MIN_INTERVAL_S: Final[float] = 200.0


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
        #: Set by the HUNG edge; :meth:`check_once` then reads the baseline token
        #: OUTSIDE the lock on the SAME poll (the probe ``os.stat``s a possibly-wedged
        #: volume, so it must never run while holding ``_lock`` — the stalled-probe-
        #: under-lock class DEC-SURFACE-TRIAL-001 MINOR-1 fixed in ``SummaryCache``).
        self._baseline_capture_pending = False
        #: Wedge-window restart marker: set when the escalation OBSERVED the revision
        #: token move after the HUNG edge, OR when a blind (``None``) HUNG-edge baseline
        #: was later replaced by the FIRST real token — either way BOTH halves of the
        #: DUAL signal must cover the full ``wedge_multiplier x timeout`` window from
        #: that observation before a verdict.
        self._wedge_window_restart_at: float | None = None

        #: Monotonic source for the alive-beat throttle (DEC-PULL-TELEMETRY-TRIAD-001) —
        #: the SAME injected clock the underlying watchdog reads, so tests drive both
        #: deterministically; defaults to the watchdog's own ``time.monotonic`` default.
        self._alive_clock: Callable[[], float] = clock if clock is not None else time.monotonic
        #: Throttle floor between two alive-beat INFO lines: the check cadence, but never
        #: below :data:`ALIVE_LOG_MIN_INTERVAL_S` (the ≤ 18 lines/hour bound). Recomputed
        #: by :meth:`start` when an explicit ``poll_seconds`` changes the cadence.
        self._alive_interval: float = max(self._timeout / 3.0, ALIVE_LOG_MIN_INTERVAL_S)
        #: Last alive-beat instant (``-inf`` ⇒ the FIRST check logs — an early proof of
        #: life; every later one throttles against :attr:`_alive_interval`).
        self._last_alive_log: float = float("-inf")
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
        """HEALTHY→HUNG edge: flag the wedge-baseline capture, then forward to the caller.

        The lake-revision token observed at the HUNG edge is the baseline the
        escalation compares against (DEC-PULL-WEDGE-ESCALATE-001): "no movement since
        the HUNG edge". Runs inside :meth:`check_once`'s lock (the underlying watchdog
        fires callbacks synchronously from ``check_once``), so the probe itself is NOT
        run here — a wedged ``os.stat`` under the lock would block a recovering
        worker's ``beat`` (the stalled-probe-under-lock class). :meth:`check_once`
        reads the token OUTSIDE the lock on this SAME poll and stores the baseline.
        """
        self._baseline_capture_pending = True
        self._wedge_window_restart_at = None
        self._user_on_hung(event)

    def _escalation_wants_probe_locked(self) -> bool:
        """True when THIS poll's escalation evaluation needs a revision token — lock held.

        Replicates every pre-probe guard of :meth:`_maybe_escalate_locked` so the probe
        stays LAZY: it is never touched while healthy, never without an ``on_wedged``
        hook, never before the full wedge window of silence, and never after the
        terminal latch. :meth:`check_once` calls this under the lock, releases the
        lock, reads the token, and re-takes the lock to evaluate — the probe cost
        (an ``os.stat`` on a possibly-wedged volume) is paid OUTSIDE the lock.
        """
        if self._wedged_fired or self.on_wedged is None:
            return False
        if self._wd.state_of(PULL_WORKER_NAME) is not WorkerState.HUNG:
            return False
        if self._last_beat_at is None:  # pragma: no cover — start() always seeds it
            return False
        if self._clock_fn() - self._last_beat_at < self._wedge_multiplier * self._timeout:
            return False
        return self._hung_baseline_captured

    def _maybe_escalate_locked(self, current: str | None) -> None:
        """Fire ``on_wedged`` ONCE when the DUAL wedge signal holds — caller holds the lock.

        DEC-PULL-WEDGE-ESCALATE-001, STAGE 1. *current* is the revision token
        :meth:`check_once` read OUTSIDE the lock on this poll (never probed here — the
        stalled-probe-under-lock guard). Escalates ONLY when ALL hold:

        * the worker is currently HUNG and the escalation has not already fired (the
          terminal latch — a later heartbeat never un-latches a fired escalation);
        * ZERO heartbeats for ``wedge_multiplier × timeout`` (the last-beat age — a
          beat between polls resets it, so any sign of life defers the verdict);
        * the DUAL second half CONFIRMS: a revision probe is wired, a baseline is
          known, BOTH tokens are real (an absent/erroring probe ABSTAINS — never
          escalate on a blind probe), and the token has NOT moved. A token that DID
          move re-baselines (the worker is writing — measure any later freeze against
          the newest observation). A ``None`` HUNG-edge baseline (the probe erred at
          the edge — likely the SAME wedged volume) is REPLACED by the first later
          real token, restarting the wedge window from that observation, so a
          recovered probe re-arms the verdict instead of abstaining forever.

        O(1) per poll. The hook is best-effort (swallow + log), mirroring
        :meth:`_recovery`.
        """
        if not self._escalation_wants_probe_locked():
            # Guards re-checked under THIS lock hold: a beat may have landed while the
            # probe ran outside the lock — any sign of life defers the verdict.
            return
        if current is None:
            # The probe abstained on this poll: the DUAL signal cannot be confirmed —
            # never escalate on heartbeat silence alone (honest envelope: a probe-less
            # deployment never reaches WEDGED).
            return
        if self._hung_baseline_token is None:
            # The HUNG-edge probe abstained (baseline None — e.g. the wedged volume also
            # wedged the edge's os.stat). Adopt the FIRST later real token as the fresh
            # baseline and restart the wedge window from this observation — the verdict
            # is re-armed once the probe recovers, never fired against a blind baseline.
            self._hung_baseline_token = current
            self._wedge_window_restart_at = self._clock_fn()
            return
        if current != self._hung_baseline_token:
            # The lake moved — the worker IS making durable progress despite the silent
            # heartbeat. Re-baseline so a LATER freeze is measured from this observation,
            # and restart the wedge window from the movement (both DUAL halves must cover
            # the FULL window before a verdict).
            self._hung_baseline_token = current
            self._wedge_window_restart_at = self._clock_fn()
            return
        if self._wedge_window_restart_at is not None and (
            self._clock_fn() - self._wedge_window_restart_at
            < self._wedge_multiplier * self._timeout
        ):
            # The window restarted (observed movement / adopted baseline) and the freeze
            # since then has not yet spanned the full wedge window — defer the verdict.
            return
        hook = self.on_wedged
        last_beat = self._last_beat_at
        if hook is None or last_beat is None:  # pragma: no cover — guarded above
            return  # narrowing only — _escalation_wants_probe_locked() already refused
        age = self._clock_fn() - last_beat
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
        # Re-derive the alive-beat throttle from the ACTUAL cadence (an explicit
        # ``poll_seconds`` may differ from the ``timeout / 3`` default), floored at
        # :data:`ALIVE_LOG_MIN_INTERVAL_S` so the ≤ 18 lines/hour bound always holds
        # (DEC-PULL-TELEMETRY-TRIAD-001 — one throttled INFO per check tick, no new thread).
        self._alive_interval = max(interval, ALIVE_LOG_MIN_INTERVAL_S)
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
        """Poll the heartbeat, firing the hung edge if the worker wedged — O(1).

        The lake-revision probe is read OUTSIDE ``_lock`` (review MINOR fold, the
        stalled-probe-under-lock class DEC-SURFACE-TRIAL-001 MINOR-1 fixed in
        ``SummaryCache``): the probe ``os.stat``s a possibly-wedged external volume —
        exactly the volume this feature suspects — so running it under the lock could
        wedge the timer thread WHILE HOLDING THE LOCK and block a recovering worker's
        :meth:`beat`. The lock is taken to decide WHETHER a token is needed (the lazy
        probe guards — never while healthy, never without a hook), released for the
        read, then re-taken to evaluate; the guards are re-checked under the second
        hold so a beat landing during the read still defers the verdict. A wedged
        probe can now stall only the timer thread itself (escalation degrades to
        abstention) — never the worker's heartbeat path.

        Also emits the THROTTLED ``pull_stall.watchdog_alive`` INFO
        (DEC-PULL-TELEMETRY-TRIAD-001): the happy path was previously SILENT (only stall
        edges logged), so post-mortem forensics could not tell "the watchdog was alive
        and saw a healthy worker" from "the watchdog never ran". One line at most per
        :attr:`_alive_interval` (≥ :data:`ALIVE_LOG_MIN_INTERVAL_S` — ≤ 18 lines/hour,
        derived from the existing check cadence; NO new thread), carrying the worker's
        current state + the audited timeout. Logging only — the underlying
        ``Watchdog.check_once`` call, its returned events and every edge callback are
        byte-identical.
        """
        with self._lock:
            events = self._wd.check_once()
            # Wedged-worker escalation (DEC-PULL-WEDGE-ESCALATE-001): evaluated on the
            # SAME poll cadence, after the HUNG/recovery edges have fired.
            capture_baseline = self._baseline_capture_pending
            want_probe = capture_baseline or self._escalation_wants_probe_locked()
            alive = self._alive_payload_locked()
        if want_probe:
            token = self._probe_token()  # OUTSIDE the lock — may block, never holds it
            with self._lock:
                if self._baseline_capture_pending:
                    # The HUNG edge on THIS poll flagged the capture: this token is the
                    # baseline the escalation compares against ("no movement since the
                    # HUNG edge"). The escalation itself starts on the NEXT poll.
                    self._hung_baseline_token = token
                    self._hung_baseline_captured = True
                    self._baseline_capture_pending = False
                else:
                    self._maybe_escalate_locked(token)
        if alive is not None:
            _LOG.info("pull_stall.watchdog_alive", **alive)
        return events

    def _alive_payload_locked(self) -> dict[str, object] | None:
        """Return the alive-beat payload when the throttle allows one, else ``None``.

        Caller holds ``self._lock``. Reads the SAME clock the watchdog uses; stamps the
        throttle baseline only when a line will actually be emitted. A never-``start``-ed
        wrapper (a bare ``check_once`` in a test) reads ``state="unregistered"`` honestly
        instead of raising off :meth:`Watchdog.state_of`'s KeyError.
        """
        now = self._alive_clock()
        if now - self._last_alive_log < self._alive_interval:
            return None
        self._last_alive_log = now
        state = (
            self._wd.state_of(PULL_WORKER_NAME).value
            if PULL_WORKER_NAME in self._wd.workers()
            else "unregistered"
        )
        return {
            "state": state,
            "timeout_seconds": self._timeout,
            "interval_seconds": self._alive_interval,
        }

    def stop(self) -> None:
        """Stop the background poll loop (called from the worker's ``finally``)."""
        self._stop.set()

    def state(self) -> WorkerState:
        """Return the worker's current state (HEALTHY / HUNG) — test accessor."""
        with self._lock:
            return self._wd.state_of(PULL_WORKER_NAME)


__all__ = [
    "ALIVE_LOG_MIN_INTERVAL_S",
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

"""Event-loop liveness watchdog (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, G5).

The 2026-07-11 heavy-pull incident's WORST symptom was 36 minutes of TOTAL
dashboard silence — the uvicorn event loop parked on synchronous work while
every poll surface aged into "STALE … retrying" with **zero server-side
signal**. The structural fixes (#1316/#1362 offloads, the 32-token lake
limiter) close the known parking edges, but nothing OBSERVED the loop itself:
a future regression of the same class would again be visible only client-side.

This module is that instrument — observe + alert ONLY, never remediation:

* a **heartbeat** ``asyncio`` task stamps a monotonic timestamp every
  ``beat_interval_s`` (1 s) — the ONLY on-loop footprint, O(1) per beat;
* a sibling **monitor daemon thread** reads the stamp every
  ``check_interval_s`` and drives a PURE state machine (:func:`check_once` —
  injectable clock, 100 % branch-testable, no real sleeps in tests):

  - lag ≥ ``warn_after_s`` (5 s — the #1316 measured breaker magnitude) →
    ONE ``loop_watchdog.stalled`` WARN per episode;
  - lag ≥ ``critical_after_s`` (30 s — the rate widget's own operator-visible
    ``STALE_RED_MS`` line) → ONE ERROR + a best-effort
    :meth:`~brutex.notifications.sweep_alerts.SweepAlerter.loop_stalled`
    CRITICAL Telegram (silent no-op when Telegram is disabled);
  - while stalled: re-LOG ≤ once per ``relog_every_s`` (60 s) with the growing
    lag, re-ALERT ≤ once per ``realert_every_s`` (300 s) — in-process durable
    dedupe (the state machine's stamps, not a log filter);
  - recovery → ONE ``loop_watchdog.recovered`` INFO carrying the total stall
    duration (measured beat-to-beat — the stalled beat to the resumed beat).

* ``/metrics``: gauge ``dashboard_loop_stall_seconds`` (current heartbeat lag,
  set every sample) + counter ``dashboard_loop_stalls_total`` (stall episodes,
  incremented on the WARN edge) via the existing
  :class:`~brutex.observability.metrics.MetricRegistry` (``None`` tolerated —
  metrics are best-effort, a registry fault never stops the watchdog).

HONEST envelope (Golden Rule #10): the watchdog detects loop PARKING (the
Edge-A silence class) and GIL-heavy phases (the monitor thread still gets GIL
slices); a SIGSTOP'd / frozen PROCESS, or a C extension holding the GIL
without release, silences the monitor too and logs NOTHING — that class is
the external healthchecks lane's job, disclosed not claimed. Healthy overhead
is ~2 wakeups/s, O(1) each — stated, not "free".

Wiring: :func:`brutex.dashboard.app.build_app` takes ``loop_watchdog=None``
(default ``None`` ⇒ ZERO threads/tasks/lifespan — the test suite's hundreds of
``build_app()`` constructions are byte-identical) and, when given one, rides a
minimal LIFESPAN (``FastAPI(lifespan=...)`` — the app's ONE lifecycle hook on
the installed dep set: Starlette 1.x REMOVED ``add_event_handler``/``on_event``,
so there are no startup/shutdown event handlers to register): serve start
launches the heartbeat task + monitor daemon, shutdown stops them cleanly
(round-3 review — this paragraph previously claimed the retired event-handler
seam; the lifespan IS the wiring). Production binds it in
``cli.cmd_dashboard`` behind the fail-loud :data:`LOOP_WATCHDOG_ENV` kill
switch (default ON; junk raises at startup — the DEC-RAM-BUDGETS-001 env
convention via :func:`resolve_loop_watchdog_enabled`).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Protocol

import structlog

if TYPE_CHECKING:
    from brutex.observability.metrics import MetricRegistry

_LOG = structlog.get_logger(__name__)

__all__ = [
    "CRITICAL_AFTER_S_DEFAULT",
    "LOOP_WATCHDOG_ENV",
    "REALERT_EVERY_S_DEFAULT",
    "RELOG_EVERY_S_DEFAULT",
    "STALL_EPISODES_COUNTER",
    "STALL_GAUGE",
    "TIER_CRITICAL",
    "TIER_OK",
    "TIER_WARNED",
    "WARN_AFTER_S_DEFAULT",
    "LoopWatchdog",
    "WatchdogState",
    "WatchdogVerdict",
    "check_once",
    "resolve_loop_watchdog_enabled",
]

#: The production kill switch — default ON (observing the loop is the safe
#: posture); junk raises at startup, never a silent coercion.
LOOP_WATCHDOG_ENV: Final[str] = "BRUTEX_LOOP_WATCHDOG"

_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true", "yes"})
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})

#: WARN threshold: 5 s of heartbeat lag. The #1316 real-uvicorn breaker
#: measured ONE ~5 s stalled on-loop probe freezing a concurrent /state poll
#: to ~5 s — the smallest magnitude that is unambiguously "the loop is not
#: serving pollers", above any healthy scheduling jitter.
WARN_AFTER_S_DEFAULT: Final[float] = 5.0

#: CRITICAL threshold: 30 s — the rate widget's own ``STALE_RED_MS``
#: (bx-rate-monitor.js), i.e. the exact line at which the OPERATOR sees a red
#: "STALE"; crossing it server-side is by definition operator-visible.
CRITICAL_AFTER_S_DEFAULT: Final[float] = 30.0

#: While stalled, re-log at most once per this many seconds (growing lag).
RELOG_EVERY_S_DEFAULT: Final[float] = 60.0

#: While CRITICAL persists, re-alert (Telegram) at most once per this many
#: seconds — in-process durable dedupe via the state machine's stamp.
REALERT_EVERY_S_DEFAULT: Final[float] = 300.0

#: /metrics names (registered idempotently on start; None registry tolerated).
STALL_GAUGE: Final[str] = "dashboard_loop_stall_seconds"
STALL_EPISODES_COUNTER: Final[str] = "dashboard_loop_stalls_total"

#: State-machine tiers (plain strings — stable log/test vocabulary).
TIER_OK: Final[str] = "ok"
TIER_WARNED: Final[str] = "warned"
TIER_CRITICAL: Final[str] = "critical"

#: Monitor-thread join bound on :meth:`LoopWatchdog.stop` — shutdown must
#: never hang on a wedged monitor (the thread is a daemon regardless).
_STOP_JOIN_TIMEOUT_S: Final[float] = 2.0


def resolve_loop_watchdog_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the :data:`LOOP_WATCHDOG_ENV` kill switch — fail-loud.

    unset / ``""`` / ``"1"`` / ``"on"`` / ``"true"`` / ``"yes"``
    (case-insensitive, whitespace-stripped) → ``True`` (default-ENABLED —
    observing the loop is the safe posture); ``"0"`` / ``"off"`` / ``"false"``
    / ``"no"`` → ``False``. Anything else raises — junk is never silently
    coerced (the DEC-RAM-BUDGETS-001 fail-loud env convention, the
    ``resolve_market_hours_guard_enabled`` template), so a typo can never
    SILENTLY disable the watchdog.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is set to anything outside the two token sets.
    """
    import os

    mapping = os.environ if env is None else env
    raw = mapping.get(LOOP_WATCHDOG_ENV, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{LOOP_WATCHDOG_ENV}={raw!r} is not a recognised switch value; "
        f"expected one of '1', 'on', 'true', 'yes' (enable — the default when unset) "
        f"or '0', 'off', 'false', 'no' (disable)"
    )


class AlerterLike(Protocol):
    """The one alert hook the watchdog needs (SweepAlerter satisfies it)."""

    def loop_stalled(self, *, stalled_s: float, threshold_s: float) -> bool:
        """Fire the CRITICAL loop-stall alert (best-effort)."""
        ...  # pragma: no cover — Protocol body


@dataclass(frozen=True, slots=True)
class WatchdogState:
    """The monitor's persistent state between samples — immutable, replaced per verdict.

    ``stall_started_beat`` holds the LAST-BEAT value observed when the stall
    episode began (the true stall start — the beat that went stale; it cannot
    move while the loop is parked), so recovery can report the total stall as
    ``fresh beat − stalled beat`` without ever seeing the resume instant
    directly. ``episodes`` counts WARN edges (feeds the /metrics counter).
    """

    tier: str = TIER_OK
    stall_started_beat: float = 0.0
    last_logged_at: float = 0.0
    last_alerted_at: float = 0.0
    episodes: int = 0


@dataclass(frozen=True, slots=True)
class WatchdogVerdict:
    """One :func:`check_once` decision — the new state plus the edge flags.

    Exactly the actions the driver must take: ``warn_edge`` (episode start —
    WARN log + episode counter), ``critical_edge`` (ERROR log + Telegram),
    ``relog`` (periodic while stalled), ``realert`` (periodic while critical),
    ``recovered`` (INFO with ``stall_total_s``). ``lag_s`` is the sampled
    heartbeat lag (never negative — a non-monotonic clock clamps to 0.0).
    """

    state: WatchdogState
    lag_s: float
    warn_edge: bool = False
    critical_edge: bool = False
    relog: bool = False
    realert: bool = False
    recovered: bool = False
    stall_total_s: float = 0.0


def check_once(
    now: float,
    last_beat: float,
    state: WatchdogState,
    *,
    warn_after_s: float = WARN_AFTER_S_DEFAULT,
    critical_after_s: float = CRITICAL_AFTER_S_DEFAULT,
    relog_every_s: float = RELOG_EVERY_S_DEFAULT,
    realert_every_s: float = REALERT_EVERY_S_DEFAULT,
) -> WatchdogVerdict:
    """One PURE watchdog decision: ``(now, last_beat, state) → verdict``.

    The whole OK→WARNED→CRITICAL→recovered state machine lives here —
    injectable clock values, zero I/O, zero sleeps — so every branch is
    deterministically testable. Laws:

    * ``lag = max(0.0, now - last_beat)`` — a non-monotonic clock can never
      yield a negative lag (mirrors the #920 floor's neutralization).
    * Thresholds are ``>=`` (a lag exactly AT the threshold trips — a stall
      is never under-reported by a boundary tie).
    * A monitor thread that was ITSELF starved may observe the lag jumping
      straight past ``critical_after_s`` — that sample fires the warn edge
      AND the critical edge together (one episode, both actions).
    * ``relog`` fires only on a NON-edge stalled sample ≥ ``relog_every_s``
      after the last logged sample; ``realert`` only at CRITICAL tier on a
      non-critical-edge sample ≥ ``realert_every_s`` after the last alert.
    * Recovery (lag back under ``warn_after_s``) resets to a fresh
      :class:`WatchdogState` (keeping ``episodes``) and reports
      ``stall_total_s = max(0.0, last_beat - stall_started_beat)`` — the
      resumed beat minus the stalled beat, honest to within one beat interval.
    """
    lag = max(0.0, now - last_beat)
    if lag >= critical_after_s:
        target = TIER_CRITICAL
    elif lag >= warn_after_s:
        target = TIER_WARNED
    else:
        target = TIER_OK

    if target == TIER_OK:
        if state.tier == TIER_OK:
            return WatchdogVerdict(state=state, lag_s=lag)
        total = max(0.0, last_beat - state.stall_started_beat)
        return WatchdogVerdict(
            state=WatchdogState(episodes=state.episodes),
            lag_s=lag,
            recovered=True,
            stall_total_s=total,
        )

    warn_edge = state.tier == TIER_OK
    critical_edge = target == TIER_CRITICAL and state.tier != TIER_CRITICAL
    relog = not warn_edge and not critical_edge and now - state.last_logged_at >= relog_every_s
    realert = (
        target == TIER_CRITICAL
        and not critical_edge
        and now - state.last_alerted_at >= realert_every_s
    )
    new_state = replace(
        state,
        tier=target,
        stall_started_beat=last_beat if warn_edge else state.stall_started_beat,
        last_logged_at=now if (warn_edge or critical_edge or relog) else state.last_logged_at,
        last_alerted_at=now if (critical_edge or realert) else state.last_alerted_at,
        episodes=state.episodes + 1 if warn_edge else state.episodes,
    )
    return WatchdogVerdict(
        state=new_state,
        lag_s=lag,
        warn_edge=warn_edge,
        critical_edge=critical_edge,
        relog=relog,
        realert=realert,
    )


class LoopWatchdog:
    """The heartbeat-task + monitor-thread pair around :func:`check_once`.

    Parameters
    ----------
    clock
        Zero-arg monotonic time source (injectable for tests).
    beat_interval_s, check_interval_s
        Heartbeat stamp / monitor sample cadences (both 1 s by default).
    warn_after_s, critical_after_s, relog_every_s, realert_every_s
        The :func:`check_once` thresholds (see its docstring for the laws).
    alerter
        Optional :class:`AlerterLike` (production: a ``SweepAlerter``) —
        best-effort: a RAISING alerter is swallowed with one exception log,
        the watchdog never crashes (and the SweepAlerter itself is already a
        silent no-op when Telegram is disabled).
    registry
        Optional :class:`~brutex.observability.metrics.MetricRegistry`;
        ``None`` skips metrics entirely. Metric faults are swallowed
        (best-effort — observation must never break observation).
    wait_fn
        Injectable monitor wait: ``wait_fn(timeout) -> True`` means STOP.
        ``None`` uses the internal stop event's ``wait`` (production). Tests
        inject a scripted wait to drive exact sample counts with no sleeps.

    Raises
    ------
    ValueError
        On a non-positive/non-finite interval or an inverted threshold pair
        (fail-loud construction — junk config never runs silently skewed).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        beat_interval_s: float = 1.0,
        check_interval_s: float = 1.0,
        warn_after_s: float = WARN_AFTER_S_DEFAULT,
        critical_after_s: float = CRITICAL_AFTER_S_DEFAULT,
        relog_every_s: float = RELOG_EVERY_S_DEFAULT,
        realert_every_s: float = REALERT_EVERY_S_DEFAULT,
        alerter: AlerterLike | None = None,
        registry: MetricRegistry | None = None,
        wait_fn: Callable[[float], bool] | None = None,
    ) -> None:
        for name, value in (
            ("beat_interval_s", beat_interval_s),
            ("check_interval_s", check_interval_s),
            ("warn_after_s", warn_after_s),
            ("critical_after_s", critical_after_s),
            ("relog_every_s", relog_every_s),
            ("realert_every_s", realert_every_s),
        ):
            # NaN fails both comparisons' complement — an explicit finite+positive law.
            if not (value > 0.0 and value != float("inf")):
                raise ValueError(f"{name} must be a positive finite number, got {value!r}")
        if critical_after_s < warn_after_s:
            raise ValueError(
                f"critical_after_s ({critical_after_s}) must be >= warn_after_s ({warn_after_s})"
            )
        self._clock = clock
        self._beat_interval_s = beat_interval_s
        self._check_interval_s = check_interval_s
        self._warn_after_s = warn_after_s
        self._critical_after_s = critical_after_s
        self._relog_every_s = relog_every_s
        self._realert_every_s = realert_every_s
        self._alerter = alerter
        self._registry = registry
        self._stop_event = threading.Event()
        self._wait_fn = wait_fn if wait_fn is not None else self._stop_event.wait
        self._beat_lock = threading.Lock()
        self._last_beat: float = 0.0
        self._state = WatchdogState()
        self._task: asyncio.Task[None] | None = None
        self._monitor: threading.Thread | None = None
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start_from_running_loop(self) -> None:
        """FastAPI ``startup`` handler — start on the CURRENT running loop."""
        self.start(asyncio.get_running_loop())

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the heartbeat task on *loop* + the monitor daemon thread.

        Idempotent (a second call is a no-op). A monitor ``Thread.start()``
        failure (thread exhaustion) cancels the just-created heartbeat task,
        logs ONE ``loop_watchdog.start_failed`` WARN, and leaves the watchdog
        INERT — boot never crashes (the liveproof/best-effort bind contract).
        """
        if self._started:
            return
        # Re-arm after a prior stop(): the stop event stays set across the
        # cycle, and the default monitor wait IS `_stop_event.wait` — without
        # the clear, a restarted monitor's FIRST wait returns True and the
        # thread exits immediately (a silently-dead watchdog; round-1 nit).
        self._stop_event.clear()
        with self._beat_lock:
            self._last_beat = self._clock()
        task = loop.create_task(self._heartbeat(), name="bx-loop-watchdog-heartbeat")
        monitor = threading.Thread(
            target=self._monitor_loop, daemon=True, name="bx-loop-watchdog-monitor"
        )
        try:
            monitor.start()
        except Exception as exc:  # pylint: disable=broad-except — boot never crashes
            task.cancel()
            _LOG.warning("loop_watchdog.start_failed", error=repr(exc))
            return
        self._task = task
        self._monitor = monitor
        self._started = True
        self._register_metrics()
        _LOG.info(
            "loop_watchdog.started",
            beat_interval_s=self._beat_interval_s,
            warn_after_s=self._warn_after_s,
            critical_after_s=self._critical_after_s,
        )

    def stop(self) -> None:
        """FastAPI ``shutdown`` handler — cancel the task, join the monitor.

        The join is BOUNDED (:data:`_STOP_JOIN_TIMEOUT_S`) so shutdown can
        never hang on a wedged monitor (the thread is a daemon regardless);
        idempotent and safe on a never-started watchdog.
        """
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        monitor = self._monitor
        if monitor is not None:
            monitor.join(_STOP_JOIN_TIMEOUT_S)
            self._monitor = None
        self._started = False

    @property
    def started(self) -> bool:
        """Whether the pair is currently running — O(1) (tests + census pins)."""
        return self._started

    # ── Heartbeat (the ONLY on-loop footprint) ───────────────────────

    async def _heartbeat(self) -> None:
        """Stamp the monotonic beat every ``beat_interval_s`` until cancelled."""
        while True:
            with self._beat_lock:
                self._last_beat = self._clock()
            await asyncio.sleep(self._beat_interval_s)

    # ── Monitor (daemon thread) ──────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Sample the beat every ``check_interval_s`` until the stop event."""
        while not self._wait_fn(self._check_interval_s):
            self.sample_once()

    def sample_once(self) -> WatchdogVerdict:
        """One monitor sample: read the beat, run :func:`check_once`, act.

        Public so tests (and the scripted ``wait_fn`` harness) can drive exact
        sample sequences deterministically. Every side effect (log / metric /
        alert) is best-effort — a fault in one never skips the others or
        crashes the monitor.
        """
        now = self._clock()
        with self._beat_lock:
            last_beat = self._last_beat
        verdict = check_once(
            now,
            last_beat,
            self._state,
            warn_after_s=self._warn_after_s,
            critical_after_s=self._critical_after_s,
            relog_every_s=self._relog_every_s,
            realert_every_s=self._realert_every_s,
        )
        self._state = verdict.state
        self._set_gauge(verdict.lag_s)
        if verdict.warn_edge:
            self._inc_episodes()
        if verdict.critical_edge:
            _LOG.error(
                "loop_watchdog.stalled",
                tier=TIER_CRITICAL,
                stall_s=round(verdict.lag_s, 3),
                critical_after_s=self._critical_after_s,
            )
            self._alert(verdict.lag_s)
        elif verdict.warn_edge:
            _LOG.warning(
                "loop_watchdog.stalled",
                tier=TIER_WARNED,
                stall_s=round(verdict.lag_s, 3),
                warn_after_s=self._warn_after_s,
            )
        elif verdict.relog:
            log = _LOG.error if verdict.state.tier == TIER_CRITICAL else _LOG.warning
            log(
                "loop_watchdog.stalled",
                tier=verdict.state.tier,
                stall_s=round(verdict.lag_s, 3),
                relog=True,
            )
        if verdict.realert:
            self._alert(verdict.lag_s)
        if verdict.recovered:
            _LOG.info(
                "loop_watchdog.recovered",
                stall_total_s=round(verdict.stall_total_s, 3),
            )
        return verdict

    # ── Best-effort side-effect helpers ──────────────────────────────

    def _register_metrics(self) -> None:
        """Idempotently declare the gauge + counter (None registry ⇒ no-op)."""
        if self._registry is None:
            return
        try:
            self._registry.gauge(STALL_GAUGE, "Current dashboard event-loop heartbeat lag (s)")
            self._registry.counter(
                STALL_EPISODES_COUNTER, "Dashboard event-loop stall episodes (WARN edges)"
            )
        except Exception:  # pylint: disable=broad-except — metrics are best-effort
            _LOG.exception("loop_watchdog.metrics_register_failed")

    def _set_gauge(self, lag_s: float) -> None:
        """Set the lag gauge (best-effort; None registry ⇒ no-op)."""
        if self._registry is None:
            return
        try:
            self._registry.set(STALL_GAUGE, lag_s)
        except Exception:  # pylint: disable=broad-except — metrics are best-effort
            _LOG.exception("loop_watchdog.metrics_set_failed")

    def _inc_episodes(self) -> None:
        """Bump the episode counter (best-effort; None registry ⇒ no-op)."""
        if self._registry is None:
            return
        try:
            self._registry.inc(STALL_EPISODES_COUNTER)
        except Exception:  # pylint: disable=broad-except — metrics are best-effort
            _LOG.exception("loop_watchdog.metrics_inc_failed")

    def _alert(self, stalled_s: float) -> None:
        """Fire the CRITICAL Telegram (best-effort; None alerter ⇒ no-op)."""
        if self._alerter is None:
            return
        try:
            self._alerter.loop_stalled(stalled_s=stalled_s, threshold_s=self._critical_after_s)
        except Exception:  # pylint: disable=broad-except — an alert fault never stops the watchdog
            _LOG.exception("loop_watchdog.alert_failed")

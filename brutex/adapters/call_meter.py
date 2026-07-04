"""Vendor-neutral rolling meter of REAL SDK-call completions → ACHIEVED throughput.

``DEC-GROWW-RATE-ACHIEVED-METER-001``. The rate **controller** reports its TARGET
(``current_rps`` — the ceiling it is *allowing*); this meter reports what *actually
happened* — the **achieved** calls/s computed from true call-completion instants —
so the operator can SEE whether a pull is really hitting the ceiling instead of
trusting the controller's self-report. (A live 779-symbol spot pull sat at a
measured **3.4 calls/s** while ``current_rps`` read ``8.0``; the gap — the shared
SDK connection serialising the workers, avg **0.49** in-flight — was invisible
because no instrument reported the achieved rate. This resurrects the Groww twin of
the meter the dashboard rate panel already expects, deleted in the vendor
remigration.)

The meter is the single source of the **honest** number: it counts what completed,
from real timestamps, never the controller's target. Every gauge is O(1) amortized
(deque append + bounded evict — no pandas, no scan), and the meter is thread-safe so
the shared pool of fetch workers all record into one aggregate instrument.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Final

import structlog

#: Achieved-rate log channel — a throttled ``rate.achieved`` INFO so ``/logs`` shows
#: the achieved trajectory next to the controller's ``rate.snapshot`` target.
_LOG = structlog.get_logger("brutex.adapters.call_meter")

#: Rolling window (seconds) for the smoothed achieved-rate gauge (``effective_rps``).
#: Computed as ``completions-in-window / window`` (a FIXED divisor), so the gauge is
#: always ``<=`` the true rate — it ramps from 0 over the first window and reads the
#: exact rate only once the window is saturated. It can NEVER over-read (the safe
#: direction for an "are we hitting 8?" assurance — a low reading is honest, a high
#: one would be the dangerous lie).
ACHIEVED_WINDOW_S: Final[float] = 10.0

#: Instantaneous gauge window — the trailing-1 s completion count (``rps_1s``) and its
#: run peak (``rps_1s_max``). ``rps_1s_max`` is the live PROOF the cap held: it can
#: never exceed the ceiling + burst, so a breach would show here immediately.
INSTANT_WINDOW_S: Final[float] = 1.0

#: Bounded most-recent-N success-latency ring for the p95 gauge (O(1) memory).
LATENCY_SAMPLES: Final[int] = 256

#: Minimum seconds between throttled ``rate.achieved`` ``/logs`` lines (mirrors the
#: controller's ``rate.snapshot`` 15 s throttle so the two are comparable + neither
#: floods the log).
ACHIEVED_LOG_INTERVAL_S: Final[float] = 15.0


class CallMeter:
    """Thread-safe rolling meter of true SDK-call completions → achieved throughput.

    Wire :meth:`start` immediately before each physical call and :meth:`stop` once it
    returns (``completed=True``) or fails (``completed=False``). :meth:`snapshot`
    returns the dashboard rate-panel contract (``effective_rps`` / ``latency_ms_p95``
    / ``rps_1s`` / ``rps_1s_max``) plus the honest extras ``rate_achieved`` (alias of
    ``effective_rps``) and ``in_flight`` (the live concurrency — the gauge that makes
    the 0.49-serialised-SDK problem visible).

    Parameters
    ----------
    clock
        Monotonic seconds source (injected for deterministic tests).
    emit_log
        When ``True`` (default) a throttled ``rate.achieved`` INFO is emitted at most
        once per :data:`ACHIEVED_LOG_INTERVAL_S` of active recording; ``False``
        silences it (observability off, gauges unchanged).
    """

    __slots__ = (
        "_clock",
        "_completed_total",
        "_completions",
        "_emit_log",
        "_failed_total",
        "_failures",
        "_in_flight",
        "_issued_total",
        "_last_log",
        "_latencies",
        "_lock",
        "_rps_1s_max",
    )

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, emit_log: bool = True
    ) -> None:
        self._clock = clock
        self._emit_log = emit_log
        self._lock = threading.Lock()
        #: Completion instants within the trailing :data:`ACHIEVED_WINDOW_S` (ascending).
        self._completions: deque[float] = deque()
        #: FAILURE instants within the trailing :data:`ACHIEVED_WINDOW_S` (ascending) —
        #: the rolling twin of ``_completions`` powering the ``failed_rps`` gauge
        #: (DEC-PULL-TELEMETRY-TRIAD-001): an issuing-but-all-failing pull shows a live
        #: failed-calls rate instead of silently reading as an idle 0-throughput meter.
        self._failures: deque[float] = deque()
        #: Most-recent success latencies (ms), capped at :data:`LATENCY_SAMPLES`.
        self._latencies: deque[float] = deque()
        self._rps_1s_max: int = 0
        self._in_flight: int = 0
        #: LIFETIME totals (DEC-PULL-TELEMETRY-TRIAD-001): calls issued (``start``),
        #: completed (``stop(completed=True)``) and failed (``stop(completed=False)``)
        #: over this meter's whole life — the honest accounting that lets a burned-call
        #: burst (e.g. the 06:00 token-reset blip) be READ instead of inferred.
        #: Monotonic integers, O(1) to maintain, never reset.
        self._issued_total: int = 0
        self._completed_total: int = 0
        self._failed_total: int = 0
        #: Throttle baseline; stamped ``now`` at construction so the FIRST completion
        #: does not immediately log (warm-up grace, like the controller snapshot).
        self._last_log: float = clock()

    def start(self) -> None:
        """Mark one physical call as in-flight (``in_flight += 1``; ``issued += 1``). O(1)."""
        with self._lock:
            self._in_flight += 1
            self._issued_total += 1

    def stop(self, *, elapsed_ms: float, completed: bool) -> None:
        """Mark one in-flight call done (``in_flight -= 1``); a *completed* call counts.

        A ``completed=True`` call appends its completion instant + latency (it is an
        ACHIEVED call — what the achieved-rate gauges measure); a ``completed=False``
        call (a retryable failure that backed off / a terminal error) releases the
        in-flight slot AND is counted into the ``failed`` lifetime total + the rolling
        ``failed_rps`` window (DEC-PULL-TELEMETRY-TRIAD-001) — it never feeds the
        achieved gauges, so the achieved rate stays SUCCESSFUL throughput, never
        inflated by failed attempts. Emits the throttled ``rate.achieved`` log when an
        interval has elapsed. O(1) amortized.
        """
        now = self._clock()
        payload: dict[str, Any] | None = None
        with self._lock:
            # Floor at 0 — a stray stop() (double-stop / a defect) can never drive the
            # dashboard concurrency gauge negative (defensive; the GrowwClient try/finally
            # bracket already pairs every start with exactly one stop).
            self._in_flight = max(0, self._in_flight - 1)
            if completed:
                self._completed_total += 1
                self._completions.append(now)
                self._latencies.append(elapsed_ms)
                while len(self._latencies) > LATENCY_SAMPLES:
                    self._latencies.popleft()
                self._evict_locked(now)
                rps_1s = self._count_since_locked(now - INSTANT_WINDOW_S)
                self._rps_1s_max = max(self._rps_1s_max, rps_1s)
                if self._emit_log and (now - self._last_log) >= ACHIEVED_LOG_INTERVAL_S:
                    self._last_log = now
                    payload = self._snapshot_locked(now)
            else:
                # A failed attempt is COUNTED (lifetime total + the rolling failed_rps
                # window — DEC-PULL-TELEMETRY-TRIAD-001) but NEVER feeds the achieved
                # gauges: effective_rps / latency / rps_1s stay successful-calls-only,
                # byte-identical to the pre-decision meter.
                self._failed_total += 1
                self._failures.append(now)
                self._evict_locked(now)
        if payload is not None:
            _LOG.info("rate.achieved", **payload)

    def snapshot(self) -> dict[str, Any]:
        """Return the current gauges (the dashboard rate-panel contract + extras)."""
        now = self._clock()
        with self._lock:
            self._evict_locked(now)
            return self._snapshot_locked(now)

    # ── locked helpers (caller holds ``self._lock``) ──────────────────────

    def _evict_locked(self, now: float) -> None:
        """Drop completion + failure instants older than the trailing :data:`ACHIEVED_WINDOW_S`."""
        cutoff = now - ACHIEVED_WINDOW_S
        while self._completions and self._completions[0] < cutoff:
            self._completions.popleft()
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _count_since_locked(self, since: float) -> int:
        """Count completion instants ``>= since`` (deque is ascending → reverse-break)."""
        count = 0
        for instant in reversed(self._completions):
            if instant >= since:
                count += 1
            else:
                break
        return count

    def _p95_locked(self) -> float:
        """p95 of the bounded recent-latency ring (ms), or ``0.0`` when empty."""
        if not self._latencies:
            return 0.0
        ordered = sorted(self._latencies)
        idx = min(len(ordered) - 1, int(0.95 * len(ordered)))
        return round(ordered[idx], 1)

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        """Build the gauge mapping from the (already-evicted) rolling state."""
        # effective_rps: trailing-window completions / the FIXED window — ramps from 0
        # over the first window then an exact rolling average; never over-reads.
        effective = len(self._completions) / ACHIEVED_WINDOW_S
        rps_1s = self._count_since_locked(now - INSTANT_WINDOW_S)
        return {
            "effective_rps": round(effective, 2),
            "rate_achieved": round(effective, 2),
            "rps_1s": rps_1s,
            "rps_1s_max": self._rps_1s_max,
            "latency_ms_p95": self._p95_locked(),
            "in_flight": self._in_flight,
            # ADDITIVE keys only (DEC-PULL-TELEMETRY-TRIAD-001) — the six keys above are
            # the pre-existing dashboard rate-panel contract and stay byte-identical;
            # consumers opt into the failure telemetry by reading these new keys.
            "issued": self._issued_total,
            "completed": self._completed_total,
            "failed": self._failed_total,
            # Rolling failed-calls rate over the SAME fixed window as effective_rps —
            # like it, a ramp-from-0 average that can never over-read.
            "failed_rps": round(len(self._failures) / ACHIEVED_WINDOW_S, 2),
        }

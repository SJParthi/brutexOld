"""O(1)-space throughput telemetry for the two-phase universe pull.

The expiries / contracts phases fan a fetch out across a worker pool under a
shared token bucket (``rate_common`` + ``rate_limiter``). Whether the
phase actually reaches the bucket's target r/s depends on whether the pool is
kept saturated: if every worker is blocked on a slow network round-trip, the
achieved rate sits *below* the bucket ceiling (the live 2026-05-30 expiries run
measured 2.75 r/s against an 8 r/s ceiling — latency-bound, not rate-bound).

:class:`PhaseThroughput` makes that observable. A worker wraps each call in
:meth:`track`; the aggregator records the call's wall-duration and the live
in-flight count, and :meth:`snapshot` reports:

* ``achieved_rps`` — completed calls ÷ wall-clock elapsed (the number the
  operator wants pinned at the bucket ceiling),
* ``latency_mean_s`` / ``latency_min_s`` / ``latency_max_s`` — per-call
  round-trip, the term that makes the phase latency-bound, and
* ``peak_in_flight`` — the high-water concurrency. ``peak_in_flight ==
  max_workers`` means the pool is the bottleneck (raise fan-out); a lower peak
  means the bucket (or the work list) is.

It is **O(1) space** (Golden Rule #5 / charter): it keeps running scalars
(count, latency sum, min, max, live + peak in-flight) — never a per-call list —
so it scales identically for the 1.6 k-cell expiries phase and the ~19 k-job
contracts phase. It is **thread-safe** (one lock; the lock is never held across
the tracked call itself, only around the O(1) counter updates), so N pool
workers record concurrently without contention on the network path.
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

#: Reported when no call has completed yet (avoids 0/0 — an empty phase is a
#: valid, non-erroring outcome, e.g. every cell skipped by the O(1) watermark).
_EMPTY: Final[float] = 0.0


@dataclass(frozen=True, slots=True)
class ThroughputSnapshot:
    """An immutable point-in-time view of a phase's throughput.

    All times are seconds. ``achieved_rps`` is the headline metric; the latency
    + concurrency fields explain *why* it is where it is (latency-bound vs
    rate-bound vs pool-bound).
    """

    completed: int
    wall_elapsed_s: float
    achieved_rps: float
    latency_mean_s: float
    latency_min_s: float
    latency_max_s: float
    peak_in_flight: int

    def as_log_fields(self) -> dict[str, float | int]:
        """Render the snapshot as rounded ``structlog`` key/value fields.

        Returns
        -------
        dict[str, float | int]
            Fields suitable for one ``*.done`` audit line — counts as ``int``,
            seconds/rates rounded to 3 decimals for log readability (display
            only; the stored snapshot keeps full precision).
        """
        return {
            "completed": self.completed,
            "wall_elapsed_s": round(self.wall_elapsed_s, 3),
            "achieved_rps": round(self.achieved_rps, 3),
            "latency_mean_s": round(self.latency_mean_s, 3),
            "latency_min_s": round(self.latency_min_s, 3),
            "latency_max_s": round(self.latency_max_s, 3),
            "peak_in_flight": self.peak_in_flight,
        }


class PhaseThroughput:
    """Thread-safe, O(1)-space achieved-r/s + latency + saturation aggregator.

    Parameters
    ----------
    clock
        Monotonic seconds source. Defaults to :func:`time.monotonic` (immune to
        NTP steps); injectable so tests advance a synthetic clock deterministically.
    """

    __slots__ = (
        "_clock",
        "_completed",
        "_in_flight",
        "_latency_max",
        "_latency_min",
        "_latency_sum",
        "_lock",
        "_peak_in_flight",
        "_start",
    )

    def __init__(self, *, clock: Callable[[], float] = _time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._start = clock()
        self._completed = 0
        self._latency_sum = 0.0
        self._latency_min = 0.0
        self._latency_max = 0.0
        self._in_flight = 0
        self._peak_in_flight = 0

    @contextmanager
    def track(self) -> Generator[None, None, None]:
        """Time one call + count it toward the live + peak in-flight concurrency.

        Enter immediately before the network call, exit after it returns *or*
        raises — a failed call still consumed a worker + wall-time, so it counts
        toward latency + saturation (its absence would understate the real
        achieved load). The lock is taken only for the two O(1) bookkeeping
        steps, never across the call body, so workers run truly concurrently.
        """
        with self._lock:
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        started = self._clock()
        try:
            yield
        finally:
            duration = self._clock() - started
            with self._lock:
                self._in_flight -= 1
                if self._completed == 0:
                    self._latency_min = duration
                    self._latency_max = duration
                else:
                    self._latency_min = min(self._latency_min, duration)
                    self._latency_max = max(self._latency_max, duration)
                self._completed += 1
                self._latency_sum += duration

    def snapshot(self) -> ThroughputSnapshot:
        """Return an immutable :class:`ThroughputSnapshot` of the phase so far.

        Safe to call mid-phase or at phase end. With zero completed calls every
        rate/latency field is ``0.0`` (an empty phase never divides by zero).
        """
        with self._lock:
            completed = self._completed
            latency_sum = self._latency_sum
            latency_min = self._latency_min
            latency_max = self._latency_max
            peak_in_flight = self._peak_in_flight
            wall_elapsed = self._clock() - self._start
        if completed == 0:
            return ThroughputSnapshot(
                completed=0,
                wall_elapsed_s=wall_elapsed,
                achieved_rps=_EMPTY,
                latency_mean_s=_EMPTY,
                latency_min_s=_EMPTY,
                latency_max_s=_EMPTY,
                peak_in_flight=peak_in_flight,
            )
        achieved_rps = completed / wall_elapsed if wall_elapsed > 0.0 else _EMPTY
        return ThroughputSnapshot(
            completed=completed,
            wall_elapsed_s=wall_elapsed,
            achieved_rps=achieved_rps,
            latency_mean_s=latency_sum / completed,
            latency_min_s=latency_min,
            latency_max_s=latency_max,
            peak_in_flight=peak_in_flight,
        )


__all__ = ["PhaseThroughput", "ThroughputSnapshot"]

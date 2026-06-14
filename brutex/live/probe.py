"""Scalability + cadence diagnostics for the Groww live feed.

Two questions the operator wants answered BEFORE trusting the live candle:

1. **How many websocket connections can one API key hold, and how does #N+1
   fail?** — :func:`probe_connections` opens connections incrementally (1, 2, 3,
   …) and stops the instant one is refused, reporting the ceiling and the exact
   error. The socket-opening step is injected, so the loop is unit-tested with a
   fake that fails at a chosen N (the real opener lives in the CLI, the only
   ``# pragma: no cover``).

2. **Are ticks arriving in milliseconds or seconds?** — :func:`analyze_cadence`
   derives the inter-arrival distribution from an already-captured tick log
   (:class:`~brutex.live.store.TickRecord`), so cadence is a pure, reproducible
   function of the durable capture — no second live run needed.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from brutex.live.store import TickRecord

#: Inter-arrival thresholds (milliseconds) → cadence classification.
TICK_BY_TICK_MS = 100.0
SUB_SECOND_MS = 1000.0
ONE_PER_SEC_MS = 1500.0

#: Minimum captured records needed to compute even one inter-arrival gap.
MIN_CADENCE_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class ConnectionProbeResult:
    """Outcome of the incremental connection ramp.

    Attributes
    ----------
    max_held
        Highest number of connections open simultaneously without a failure.
    failed_at
        The ordinal of the connection that was refused, or ``None`` if the ramp
        reached *max_conns* with no failure.
    failure
        ``"{ExceptionType}: {message}"`` for the refused connection, else ``None``.
    """

    max_held: int
    failed_at: int | None
    failure: str | None

    @property
    def reached_ceiling(self) -> bool:
        """True if a connection was actually refused (a real ceiling found)."""
        return self.failed_at is not None


@dataclass(frozen=True, slots=True)
class CadenceStats:
    """Inter-arrival statistics for captured feed callbacks.

    Attributes
    ----------
    samples
        Number of captured records analysed.
    duration_seconds
        Wall-clock span from first to last record.
    mean_interval_ms, median_interval_ms, min_interval_ms, max_interval_ms
        Inter-arrival gap statistics in milliseconds (``None`` when < 2 samples).
    classification
        Human verdict: tick-by-tick / sub-second / ≈1-per-sec / multi-second.
    """

    samples: int
    duration_seconds: float
    mean_interval_ms: float | None
    median_interval_ms: float | None
    min_interval_ms: float | None
    max_interval_ms: float | None
    classification: str


def probe_connections(
    *,
    open_connection: Callable[[int], object],
    max_conns: int,
    between: Callable[[], None] = lambda: None,
) -> ConnectionProbeResult:
    """Open connections 1..*max_conns*, stopping at the first failure.

    Parameters
    ----------
    open_connection
        Called with the 1-based ordinal; must open + return a live connection or
        raise to signal refusal. Exceptions are caught and treated as the ceiling.
    max_conns
        Upper bound on the ramp.
    between
        Optional pacing hook called after each successful open (e.g. a small
        sleep so the ramp does not hammer the endpoint).

    Returns
    -------
    ConnectionProbeResult
        The ceiling held and how #N+1 failed.
    """
    held = 0
    for ordinal in range(1, max_conns + 1):
        try:
            open_connection(ordinal)
        except Exception as exc:
            return ConnectionProbeResult(
                max_held=held,
                failed_at=ordinal,
                failure=f"{type(exc).__name__}: {exc}",
            )
        held = ordinal
        between()
    return ConnectionProbeResult(max_held=held, failed_at=None, failure=None)


def _classify(median_ms: float) -> str:
    """Map a median inter-arrival gap (ms) to a human cadence verdict."""
    if median_ms < TICK_BY_TICK_MS:
        return "tick-by-tick (sub-100ms)"
    if median_ms < SUB_SECOND_MS:
        return "sub-second (multiple updates/sec)"
    if median_ms < ONE_PER_SEC_MS:
        return "≈ 1 per second (snapshot cadence)"
    return "multi-second / sparse"


def analyze_cadence(records: Sequence[TickRecord]) -> CadenceStats:
    """Compute the feed callback inter-arrival distribution from *records*.

    Each record is one feed callback; the gaps between their ``recv_ts`` answer
    "ms or seconds?". Records are sorted by ``recv_ts`` defensively.

    Returns
    -------
    CadenceStats
        With ``classification == "insufficient data (<2 samples)"`` when fewer
        than two records are supplied.
    """
    ordered = sorted(records, key=lambda r: r.recv_ts)
    samples = len(ordered)
    if samples < MIN_CADENCE_SAMPLES:
        return CadenceStats(
            samples=samples,
            duration_seconds=0.0,
            mean_interval_ms=None,
            median_interval_ms=None,
            min_interval_ms=None,
            max_interval_ms=None,
            classification="insufficient data (<2 samples)",
        )
    gaps_ms = [
        (ordered[i].recv_ts - ordered[i - 1].recv_ts).total_seconds() * 1000.0
        for i in range(1, samples)
    ]
    median_ms = statistics.median(gaps_ms)
    duration = (ordered[-1].recv_ts - ordered[0].recv_ts).total_seconds()
    return CadenceStats(
        samples=samples,
        duration_seconds=duration,
        mean_interval_ms=statistics.fmean(gaps_ms),
        median_interval_ms=median_ms,
        min_interval_ms=min(gaps_ms),
        max_interval_ms=max(gaps_ms),
        classification=_classify(median_ms),
    )


__all__ = [
    "ONE_PER_SEC_MS",
    "SUB_SECOND_MS",
    "TICK_BY_TICK_MS",
    "CadenceStats",
    "ConnectionProbeResult",
    "analyze_cadence",
    "probe_connections",
]

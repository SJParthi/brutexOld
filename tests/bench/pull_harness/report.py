"""``PullHarnessReport`` — the frozen, honest result of an at-scale harness run.

DEC-PULL-SCALE-HARNESS-001 (MIGRATION.md #929). Every gauge is the HONEST measured
number (CLAUDE.md §2 Rule 1 / Rule 10):

* ``achieved_rps`` — successful physical calls ÷ wall-clock seconds (the TRUE throughput
  the pull realised, NOT the controller's target).
* ``allowed_rps_timeavg`` — the time-weighted mean of the controller's ``current_rps``
  across the run (what the limiter was ALLOWING — usually the ceiling, dropping only on
  a 429/timeout storm). The honest denominator for the slack ratio.
* ``slack_rps`` / ``slack_ratio`` — ``allowed − achieved`` (and that over allowed). A
  BINDING limiter has near-zero slack: the pull keeps the bucket busy. Large slack means
  the pull cannot fill the rate it is allowed (the pre-Lever-2 0.49-in-flight starve).
* ``pool_idle_pct`` — fraction of wall time the pool's connections sat idle, derived from
  the sampled in-flight vs pool size: ``1 − (in_flight_p50 / pool_size)``, clamped
  ``[0, 100]``. Low = the connections stay busy.
* ``rps_1s_max`` — the meter's run-peak trailing-1 s completion count (the live PROOF the
  cap held: it can never exceed ceiling + burst).
* ``write_p50_ms`` / ``write_p99_ms`` — the per-window lake-write latency the harness
  times around ``sink.append`` (the fetch/write overlap signal).

It is a frozen dataclass with a deterministic ``seed`` field so a gate failure is
reproducible, and a small ``as_dict`` for logging / a future JSON artifact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class PullHarnessReport:
    """One at-scale pull harness run's honest measured gauges.

    All rates are calls/second; all latencies milliseconds. ``seed`` pins reproducibility.
    """

    # ── throughput (the headline slack signal) ────────────────────────────
    # ``achieved_rps`` is the ISSUED-token rate — the rate controller's governed unit —
    # so the slack ratio measures whether the limiter is BINDING. Production consumes the
    # bucket twice per window (per-window pace + client-retry pace), so the physical
    # ``get_historical_candles`` rate is ~half this; ``physical_call_rps`` reports that
    # honest second number separately (CLAUDE.md §2 Rule 10).
    achieved_rps: float
    physical_call_rps: float
    allowed_rps_timeavg: float
    slack_rps: float
    slack_ratio: float
    # ── concurrency / pool utilisation ────────────────────────────────────
    in_flight_p50: float
    in_flight_max: int
    pool_idle_pct: float
    pool_size: int
    # ── rate-cap proof + transient pressure ───────────────────────────────
    rps_1s_max: int
    rate_limit_hits: int
    timeout_hits: int
    empty_windows: int
    # ── write latency (fetch/write overlap) ───────────────────────────────
    write_p50_ms: float
    write_p99_ms: float
    # ── totals + provenance ───────────────────────────────────────────────
    physical_calls: int
    bars_appended: int
    windows: int
    wall_s: float
    seed: int

    def as_dict(self) -> dict[str, float | int]:
        """Return a flat JSON-friendly mapping (for structured logging / artifacts)."""
        return asdict(self)


def percentile(samples: list[float], pct: float) -> float:
    """Return the *pct* (0-100) percentile of *samples* (nearest-rank), 0.0 when empty.

    A tiny dependency-free helper (no numpy in a test harness gauge): sorts the samples
    and indexes the nearest rank, clamped into range. Used for the write-latency p50/p99
    and the in-flight p50, so the report needs no extra package.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[rank])


__all__ = ["PullHarnessReport", "percentile"]

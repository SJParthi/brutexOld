"""``PullScenario`` — the inputs for one at-scale pull harness run.

DEC-PULL-SCALE-HARNESS-001 (MIGRATION.md #929). A frozen, fully-explicit description of
the synthetic universe + vendor behaviour + rate envelope the harness drives, so a run
is reproducible from this one object (the ``seed`` flows into the
:class:`~tests.fakes.fake_groww_sdk.FakeGrowwSDK`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from brutex.adapters.groww.constants import (
    GROWW_RATE_LIMIT_PER_MIN,
    GROWW_RATE_LIMIT_RPS_CEILING,
)


@dataclass(frozen=True, slots=True)
class PullScenario:
    """Inputs for one harness run — synthetic universe + vendor + rate envelope.

    Parameters
    ----------
    n_contracts
        Synthetic SPOT instruments to pull (each an independent ``NSE-EQ`` symbol).
    start, end
        The half-open historical span pulled per instrument. ``depth`` (months) is
        implied by ``end - start``; the harness tiles it into 1-minute fetch windows
        exactly as production does (30-day tiles for ``1minute``).
    interval
        The fetched interval — ``"1minute"`` (the only granularity Track-1 fetches).
    pool_size
        Independent SDK connections behind ONE controller + meter (production default 4).
    ceiling_rps, ceiling_per_min
        The injected rate envelope (defaults = the production constants: 8 r/s + 500/min).
    candles_per_window, empty_rate
        Synthetic vendor payload shape (bars per window, empty-window probability).
    min_latency_s, max_latency_s
        The fake's per-call REAL latency bounds — the knob that decides whether the pool
        saturates the rate (fast vendor) or stays latency-bound (slow vendor).
    rate_limit_call_indexes, timeout_call_indexes
        1-based physical-call indexes at which to inject a 429 / timeout burst (empty by
        default — Gate 1 is the no-burst fast-vendor saturation test).
    seed
        Base RNG seed → the fake's reproducible latency/empty/price draws.
    """

    n_contracts: int = 40
    start: date = date(2024, 1, 1)
    end: date = date(2025, 1, 1)
    interval: str = "1minute"
    pool_size: int = 4
    ceiling_rps: float = GROWW_RATE_LIMIT_RPS_CEILING
    ceiling_per_min: int = GROWW_RATE_LIMIT_PER_MIN
    candles_per_window: int = 120
    empty_rate: float = 0.0
    min_latency_s: float = 0.005
    max_latency_s: float = 0.020
    rate_limit_call_indexes: tuple[int, ...] = field(default_factory=tuple)
    timeout_call_indexes: tuple[int, ...] = field(default_factory=tuple)
    seed: int = 1234


__all__ = ["PullScenario"]

"""Pool-sizing seam for the dashboard pull controllers (DEC-DHAN-PARALLEL-INGEST-001).

The dashboard threads its shared :class:`~brutex.adapters.rate_common.RateController`
into the fan-out pool sizing so the pool width tracks the controller's current rate
(Little's law). These tests cover the pure sizing seam directly — the live
controller *construction* is ``# pragma: no cover`` (needs creds/network), but the
sizing decision must be tested:

* a ``None`` controller (dry-run / injected-factory pull) → ``None`` pool, so the
  ingest spine falls back to its own default pool;
* a real controller → a :class:`~brutex.orchestrator.parallel.PoolRunParallel`
  sized to ``workers_to_saturate(current_rps, DEFAULT_LATENCY_BUDGET_S)`` — the
  SAME uniform sizing every Groww pull uses (DEC-SPOT-POOL-UNIFORM-001: the spot
  pull is NOT special-cased; the reverted spot-only 96-wide pool hung the one
  shared GrowwAPI SDK).

Whitebox: reaches the private ``_spot_run_parallel`` sizing seam + the pool's
``_max_workers`` budget on purpose (the file-level pyright directive below
silences the friend-access warning, per the codebase's whitebox-test convention).
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from brutex.adapters.rate_common import CEILING_RPS, RateController
from brutex.dashboard.pull_controller import _spot_run_parallel
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)


class TestSpotPoolSizing:
    def test_none_controller_returns_none(self) -> None:
        # Dry-run / injected-factory pull → no controller → no explicit pool
        # (run_groww_spot_ingest then uses its own default PoolRunParallel).
        assert _spot_run_parallel(None) is None

    def test_real_controller_sizes_pool_uniformly(self) -> None:
        # The default RateController ceiling is the vendor-neutral CEILING_RPS (5).
        controller = RateController(sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        # Sized to saturate the controller's current rate × the GLOBAL latency
        # budget — identical to the expiry/contract pulls (no spot-special widening).
        expected = workers_to_saturate(CEILING_RPS, DEFAULT_LATENCY_BUDGET_S)
        assert pool._max_workers == expected
        assert expected == 15  # 5 r/s × 3 s = 15

    def test_production_ceiling_stays_within_the_shared_cap(self) -> None:
        # At the live Groww 8 r/s ceiling, 8 × 3 = 24 — well under MAX_FANOUT (32),
        # the modest concurrency the shared GrowwAPI SDK is known to tolerate.
        controller = RateController(ceiling_rps=8.0, sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        assert pool._max_workers == 24

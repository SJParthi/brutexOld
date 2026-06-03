"""Pool-sizing seam for the dashboard pull controllers (DEC-DHAN-PARALLEL-INGEST-001).

The dashboard threads its shared :class:`~brutex.adapters.dhan.rate_control.DhanRateController`
into the fan-out pool sizing so the pool width tracks the controller's current
rate (Little's law). These tests cover the pure sizing seam directly — the live
controller *construction* is ``# pragma: no cover`` (needs creds/network), but
the sizing decision must be tested:

* a ``None`` controller (dry-run / injected-factory pull) → ``None`` pool, so the
  ingest spine falls back to its own default pool;
* a real controller → a :class:`~brutex.orchestrator.parallel.PoolRunParallel`
  sized to ``workers_to_saturate(current_rps, DEFAULT_LATENCY_BUDGET_S)``.

Whitebox: reaches the private ``_spot_run_parallel`` / ``_options_run_parallel``
sizing seams + the pool's ``_max_workers`` budget on purpose (the file-level
pyright directive below silences the friend-access warning, per the codebase's
whitebox-test convention).
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from brutex.adapters.dhan.rate_control import CEILING_RPS, DhanRateController
from brutex.dashboard.option_pull_controller import _options_run_parallel
from brutex.dashboard.pull_controller import _spot_run_parallel
from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    PoolRunParallel,
    workers_to_saturate,
)


class TestSpotPoolSizing:
    def test_none_controller_returns_none(self) -> None:
        # Dry-run / injected-factory pull → no controller → no explicit pool
        # (run_dhan_spot_ingest then uses its own default PoolRunParallel).
        assert _spot_run_parallel(None) is None

    def test_real_controller_sizes_pool_to_rate(self) -> None:
        controller = DhanRateController(sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        # Sized to saturate the controller's current (ceiling) rate × latency.
        expected = workers_to_saturate(CEILING_RPS, DEFAULT_LATENCY_BUDGET_S)
        # The pool's worker budget equals the rate-saturating width (5 r/s × 3 s).
        assert pool._max_workers == expected
        assert expected == 15


class TestOptionPoolSizing:
    def test_none_controller_returns_none(self) -> None:
        assert _options_run_parallel(None) is None

    def test_real_controller_sizes_pool_to_rate(self) -> None:
        controller = DhanRateController(sleep=lambda _s: None)
        pool = _options_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        expected = workers_to_saturate(CEILING_RPS, DEFAULT_LATENCY_BUDGET_S)
        assert pool._max_workers == expected
        assert expected == 15

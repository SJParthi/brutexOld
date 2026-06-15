"""Pool-sizing seam for the dashboard pull controllers.

DEC-DHAN-PARALLEL-INGEST-001 + DEC-SPOT-POOL-WIDTH-15S-001. The dashboard threads
its shared :class:`~brutex.adapters.rate_common.RateController` into the spot
fan-out pool sizing so the pool width tracks the controller's current rate AND
Groww's real per-call latency (Little's law: in-flight width = rate × latency).
These tests cover the pure sizing seam directly — the live controller
*construction* is ``# pragma: no cover`` (needs creds/network), but the sizing
decision must be tested:

* a ``None`` controller (dry-run / injected-factory pull) → ``None`` pool, so the
  ingest spine falls back to its own default pool;
* a real controller → a :class:`~brutex.orchestrator.parallel.PoolRunParallel`
  sized to ``workers_to_saturate(current_rps, _SPOT_LATENCY_BUDGET_DEFAULT_S,
  cap=_SPOT_FANOUT_CAP_DEFAULT)`` — the SPOT-specific 15 s budget (NOT the global
  3 s expiry/contract budget) so the pool is wide enough (96 at the 8 r/s ceiling)
  to keep the rate bucket — not Groww's ~15 s latency — the binding gate;
* both knobs are env-overridable, and a malformed / non-positive override falls
  back to the default (a bad env var can never zero-size the pool).

Whitebox: reaches the private ``_spot_run_parallel`` sizing seam + the pool's
``_max_workers`` budget on purpose (the file-level pyright directive below
silences the friend-access warning, per the codebase's whitebox-test convention).
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import pytest
from brutex.adapters.rate_common import CEILING_RPS, RateController
from brutex.dashboard.pull_controller import (
    _SPOT_FANOUT_CAP_DEFAULT,
    _SPOT_LATENCY_BUDGET_DEFAULT_S,
    _env_positive,
    _spot_run_parallel,
)
from brutex.orchestrator.parallel import PoolRunParallel, workers_to_saturate


class TestSpotPoolSizing:
    def test_none_controller_returns_none(self) -> None:
        # Dry-run / injected-factory pull → no controller → no explicit pool
        # (run_groww_spot_ingest then uses its own default PoolRunParallel).
        assert _spot_run_parallel(None) is None

    def test_real_controller_sizes_pool_to_spot_latency(self) -> None:
        # The default RateController ceiling is the vendor-neutral CEILING_RPS (5).
        controller = RateController(sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        # Sized to saturate current_rps × the SPOT 15 s budget, capped at 96.
        expected = workers_to_saturate(
            CEILING_RPS, _SPOT_LATENCY_BUDGET_DEFAULT_S, cap=_SPOT_FANOUT_CAP_DEFAULT
        )
        assert pool._max_workers == expected
        assert expected == 75  # 5 r/s × 15 s = 75, below the 96 cap

    def test_production_ceiling_saturates_to_the_cap(self) -> None:
        # At the live Groww 8 r/s ceiling, 8 × 15 = 120 → clamped to the 96 cap
        # (the wide pool that keeps the bucket, not the ~15 s latency, the gate).
        controller = RateController(ceiling_rps=8.0, sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        assert pool._max_workers == _SPOT_FANOUT_CAP_DEFAULT == 96

    def test_default_constants(self) -> None:
        # The spot pool is sized for Groww's real ~15 s latency, far above the
        # global 3 s expiry/contract budget — that is the whole fix.
        assert _SPOT_LATENCY_BUDGET_DEFAULT_S == 15.0
        assert _SPOT_FANOUT_CAP_DEFAULT == 96

    def test_env_overrides_widen_or_narrow_the_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The operator can dial the cap DOWN (if Groww rejects the connection count)
        # or the budget UP without a redeploy. Here: cap 16 → 8 r/s × 15 s clamps to 16.
        monkeypatch.setenv("BRUTEX_SPOT_FANOUT_CAP", "16")
        controller = RateController(ceiling_rps=8.0, sleep=lambda _s: None)
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        assert pool._max_workers == 16

    def test_env_budget_override_changes_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A 4 s budget at 5 r/s → 20 workers (below the 96 cap).
        monkeypatch.setenv("BRUTEX_SPOT_LATENCY_BUDGET_S", "4")
        controller = RateController(sleep=lambda _s: None)  # 5 r/s
        pool = _spot_run_parallel(controller)
        assert isinstance(pool, PoolRunParallel)
        assert pool._max_workers == 20


class TestEnvPositive:
    """`_env_positive` — operator-tunable knob reader that can never mis-size the pool."""

    def test_missing_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_SPOT_FANOUT_CAP", raising=False)
        assert _env_positive("BRUTEX_SPOT_FANOUT_CAP", 96.0) == 96.0

    def test_valid_override_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_X", "42.5")
        assert _env_positive("BRUTEX_X", 1.0) == 42.5

    def test_non_numeric_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_X", "not-a-number")
        assert _env_positive("BRUTEX_X", 7.0) == 7.0

    def test_non_positive_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Zero and negative are ignored — a bad value can never zero-size the pool.
        monkeypatch.setenv("BRUTEX_X", "0")
        assert _env_positive("BRUTEX_X", 9.0) == 9.0
        monkeypatch.setenv("BRUTEX_X", "-5")
        assert _env_positive("BRUTEX_X", 9.0) == 9.0

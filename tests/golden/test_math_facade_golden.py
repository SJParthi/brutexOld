"""Golden surface contract for ``brutex.math`` against the REAL Rust wheels.

DEC-STRUCTURE-CLEANUP-001 (``docs/MIGRATION.md`` #569). The unit/property tests
drive the facade with fakes; this golden test pins the *actual* re-exported
surface produced by the compiled ``fno_math`` + ``trading_core`` wheels and
proves each facade symbol resolves to the SAME object the kernel exports (the
"real-run proof"). It is skipped where the wheels are unbuildable (the local
Python 3.11 sandbox); CI 3.12 + wheels is the merge oracle.

If a kernel intentionally adds/removes an entry point, this frozen set fails —
a deliberate, reviewed surface change (update :data:`_EXPECTED_SURFACE` here).
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

# Real wheels required — skip cleanly on the wheel-free sandbox.
fno_math = pytest.importorskip("fno_math")
trading_core = pytest.importorskip("trading_core")


def _fresh_real_facade() -> Any:
    """Import a fresh ``brutex.math`` bound to the real kernels in sys.modules."""
    sys.modules.pop("brutex.math", None)
    return importlib.import_module("brutex.math")


# The frozen public surface: every kernel entry point (minus each kernel's own
# ``__version__``) plus the facade's own ``kernel_versions`` helper.
_EXPECTED_SURFACE: frozenset[str] = frozenset(
    {
        # fno_math (rust/fno-math)
        "ResidentBars",
        # DEC-ENUM-KERNEL-U128-001 — the u128 enum/count kernel twins (a
        # deliberate, reviewed surface addition; fenced additive entries).
        "ResidentBarsU128",
        "ResidentSweepSim",
        "ResidentSweepSimU128",
        "add_u64",
        "bitand_count_u64s",
        "bitcount_u64s",
        "blake3_hash",
        "count_supermask_u128s",
        "count_supermask_u64s",
        "count_supermasks_u128s",
        "count_supermasks_u64s",
        "count_supermasks_u64s_nthreads",
        "metrics_fold_recency",
        "metrics_fold_trades",
        # DEC-OPTCOST-TAILS-RUST-001 (MIGRATION.md #2054, renumbered from #1962) — the 11 optcost
        # tail-kernel pyfunctions (a deliberate, reviewed surface addition;
        # fenced additive entries — the facade rebuilds ``__all__`` from
        # ``fno_math.__all__``, so the shim's append-only optcost symbols
        # reach this frozen contract by construction).
        "optcost_atm_offset",
        "optcost_grid_around_atm",
        "optcost_history_lookup",
        "optcost_moneyness_classify",
        "optcost_next_monthly_expiry",
        "optcost_next_weekly_expiry",
        "optcost_offset_strike",
        "optcost_regime_rate",
        "optcost_round_to_atm",
        "optcost_strike_offset_from_atm",
        "optcost_strike_step",
        "popcount_bytes",
        "stats_cpcv_pass_count",
        # DEC-METRICS-RUST-STATS-002 — the stats-tail kernels (a deliberate,
        # reviewed surface addition; fenced additive entries).
        "stats_expected_max_sharpe",
        "stats_pbo_cscv",
        "stats_psr",
        "stats_wfo_pass_count",
        "sweep_drawdown_stats",
        # trading_core (rust/trading-core)
        "FIB_LEVELS_LEN",
        "GREEKS_DELTA_ADMISSION_FLOOR",
        "GREEKS_IV_CLAMP_MAX",
        "GREEKS_IV_CLAMP_MIN",
        "GREEKS_T_FLOOR_YEARS",
        "GREEKS_VEGA_ADMISSION_FLOOR",
        "IMPLEMENTED_MASK",
        "MarketStructureState",
        "PIVOT_LEVELS_LEN",
        "SPEC_SCHEMA_VERSION",
        "TOTAL_CONDITIONS",
        "VwapState",
        "compute_ema",
        "compute_fib_levels",
        "compute_pivot_levels",
        "derive_bar_metadata",
        "ema_alpha",
        "ema_seed",
        "ema_step",
        "evaluate_from_raw",
        "evaluate_spec",
        "evaluate_with_levels",
        "greeks",
        "greeks_batch",
        "minute_of_session",
        "resolve_near_month",
        "seconds_of_day",
        "spec_canonical_bytes",
        "stage1a_bar_bits_batch",
        "total_conditions",
        "version",
        "weekday_from_ordinal",
        # facade-own
        "kernel_versions",
    }
)


def test_real_facade_surface_is_frozen() -> None:
    """``__all__`` matches the frozen contract exactly (no drift)."""
    facade = _fresh_real_facade()
    assert set(facade.__all__) == _EXPECTED_SURFACE


def test_real_facade_resolves_to_kernel_objects() -> None:
    """Re-exported names ARE the real kernel objects (no copy, no shim)."""
    facade = _fresh_real_facade()
    assert facade.blake3_hash is fno_math.blake3_hash
    assert facade.popcount_bytes is fno_math.popcount_bytes
    assert facade.evaluate_with_levels is trading_core.evaluate_with_levels
    assert facade.compute_pivot_levels is trading_core.compute_pivot_levels
    assert facade.greeks is trading_core.greeks


def test_real_kernel_versions_reports_both() -> None:
    """``kernel_versions()`` reports a non-empty version per real kernel."""
    facade = _fresh_real_facade()
    versions = facade.kernel_versions()
    assert set(versions) == {"fno_math", "trading_core"}
    assert all(isinstance(v, str) and v for v in versions.values())


def test_real_facade_blake3_is_callable_through_the_facade() -> None:
    """A real call through the facade equals the direct kernel call (Golden #4)."""
    facade = _fresh_real_facade()
    assert facade.blake3_hash(b"abc") == fno_math.blake3_hash(b"abc")
    assert len(facade.blake3_hash(b"")) == 32

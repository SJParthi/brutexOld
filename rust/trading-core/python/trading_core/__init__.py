"""trading_core — shared indicator + 52-condition vocabulary kernel (PyO3 bridge).

The single source of truth for ``DEC-SHARED-CORE-001`` (``docs/MIGRATION.md`` #441).
The compiled cdylib ships as the private submodule ``trading_core._trading_core``;
this ``__init__.py`` re-exports its symbols so BruteX can write
``import trading_core; trading_core.evaluate_with_levels(...)``.

As of Phase 2b the BruteX vocabulary delegates to this wheel at runtime (the
wheel is built by maturin and installed in every CI job that imports
``brutex.vocabulary``). Exposed surface:

* ``evaluate_with_levels`` — BruteX's production per-bar evaluator (pre-built
  levels, no recompute).
* ``evaluate_from_raw`` — the parity path (recomputes pivots/fib from pdh/pdl/pdc).
* ``evaluate_spec`` — strategy-as-DATA (``DEC-STRATEGYSPEC-CORE-001``): a required-
  bits hex mask + direction over the same pre-built-levels ABI, returning
  ``(bar_bits, fires, direction)`` where ``fires = (bar_bits & mask) == mask`` —
  the backtest==live deploy contract (thin wrapper over ``evaluate_with_levels``,
  the frozen kernel untouched). ``SPEC_SCHEMA_VERSION`` / ``IMPLEMENTED_MASK`` are
  the companion constants.
* ``compute_pivot_levels`` / ``compute_fib_levels`` — the single-source indicator
  math BruteX's ``brutex.indicators`` wrappers pack into their dataclasses.
* ``compute_ema`` / ``ema_alpha`` / ``ema_seed`` / ``ema_step`` — the single-source
  EMA 20/200 recurrence BruteX's ``brutex.indicators.ema`` shim delegates to
  (``DEC-EMA-RUST-PORT-001``).
* ``greeks`` — the ONE shared Black-76 Greeks + verified Jäckel-IV seam
  (``DEC-GREEKS-ENGINE-PR2-001``): ``greeks(price, sigma, forward, strike,
  t_years, rate, is_call)`` → ``(iv, delta, gamma, theta, vega, rho, price,
  admissible)``; ``sigma=Some`` prices directly (live path), ``sigma=None``
  solves IV from the traded price first (historical path). BruteX's thin
  ``brutex.options.greeks`` wrapper delegates here; the ``GREEKS_*`` constants
  mirror the kernel's clamp / floor / admission constants.
* ``VwapState`` / ``MarketStructureState`` — the stateful, look-ahead-clean
  session-anchored VWAP + BOS/CHoCH detectors BruteX's ``stage1a`` engine drives
  per bar so the per-bar compute runs in Rust (the bit-identical
  backtest==live path; ``DEC-STAGE1A-WIRE-VWAP-BOS-001`` / ``DEC-NIFTY-INDICATORS-001``
  / ``DEC-NIFTY-BOS-CHOCH-001``).
"""

from __future__ import annotations

from ._trading_core import (  # type: ignore[import-not-found]
    FIB_LEVELS_LEN,
    GREEKS_DELTA_ADMISSION_FLOOR,
    GREEKS_IV_CLAMP_MAX,
    GREEKS_IV_CLAMP_MIN,
    GREEKS_T_FLOOR_YEARS,
    GREEKS_VEGA_ADMISSION_FLOOR,
    IMPLEMENTED_MASK,
    PIVOT_LEVELS_LEN,
    SPEC_SCHEMA_VERSION,
    TOTAL_CONDITIONS,
    MarketStructureState,
    VwapState,
    __version__,
    compute_ema,
    compute_fib_levels,
    compute_pivot_levels,
    derive_bar_metadata,
    ema_alpha,
    ema_seed,
    ema_step,
    evaluate_from_raw,
    evaluate_spec,
    evaluate_with_levels,
    greeks,
    minute_of_session,
    resolve_near_month,
    spec_canonical_bytes,
    total_conditions,
    version,
    weekday_from_ordinal,
)

__all__ = [
    "FIB_LEVELS_LEN",
    "GREEKS_DELTA_ADMISSION_FLOOR",
    "GREEKS_IV_CLAMP_MAX",
    "GREEKS_IV_CLAMP_MIN",
    "GREEKS_T_FLOOR_YEARS",
    "GREEKS_VEGA_ADMISSION_FLOOR",
    "IMPLEMENTED_MASK",
    "PIVOT_LEVELS_LEN",
    "SPEC_SCHEMA_VERSION",
    "TOTAL_CONDITIONS",
    "MarketStructureState",
    "VwapState",
    "__version__",
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
    "minute_of_session",
    "resolve_near_month",
    "spec_canonical_bytes",
    "total_conditions",
    "version",
    "weekday_from_ordinal",
]

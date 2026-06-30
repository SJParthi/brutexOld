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
* ``compute_pivot_levels`` / ``compute_fib_levels`` — the single-source indicator
  math BruteX's ``brutex.indicators`` wrappers pack into their dataclasses.
"""

from __future__ import annotations

from ._trading_core import (  # type: ignore[import-not-found]
    FIB_LEVELS_LEN,
    PIVOT_LEVELS_LEN,
    TOTAL_CONDITIONS,
    __version__,
    compute_fib_levels,
    compute_pivot_levels,
    derive_bar_metadata,
    evaluate_from_raw,
    evaluate_with_levels,
    minute_of_session,
    resolve_near_month,
    total_conditions,
    version,
    weekday_from_ordinal,
)

__all__ = [
    "FIB_LEVELS_LEN",
    "PIVOT_LEVELS_LEN",
    "TOTAL_CONDITIONS",
    "__version__",
    "compute_fib_levels",
    "compute_pivot_levels",
    "derive_bar_metadata",
    "evaluate_from_raw",
    "evaluate_with_levels",
    "minute_of_session",
    "resolve_near_month",
    "total_conditions",
    "version",
    "weekday_from_ordinal",
]

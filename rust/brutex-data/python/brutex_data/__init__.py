"""brutex_data — BruteX data pure-law kernels (PyO3 bridge).

DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940; ADR-0020 slice T10 PR-1).
The compiled cdylib ships as the private submodule
``brutex_data._brutex_data``; this ``__init__.py`` re-exports its symbols
(the fno-math DEC-P0-055 mixed layout) so callers write
``import brutex_data; brutex_data.parse_fno_symbol_coords(...)``.

The ONE consumer is the default-OFF facade
``brutex/data/fno_symbol_rust.py`` (flag ``BRUTEX_DATA_RUST``): it
guard-imports this package via ``importlib.import_module`` and treats an
absent/stale install as "stay on the Python oracle" (one loud WARN), so a
missing wheel can never break a lake walk, a movers screen, or a Greeks
discovery pass. Nothing else in ``brutex/`` imports this package — the
OPTIONAL-kernel registry (``tests/unit/test_run_builds_rust_kernels.py``)
pins exactly that.

Integers-only FFI: ``parse_fno_symbol_coords`` returns token-role SPANS
into the original Python string + the resolved expiry y/m/d + a type code
(or ``None``); Python materializes every object (string slices, ``date``,
the ``Decimal`` strike via the injected oracle helper, ``FnoSymbolParts``)
— the DEC-FEED-RUST-CORE-001 discipline.

PR-2 (DEC-DATA-RUST-PURELAW-002, MIGRATION.md #1952) adds the three
lake_keys candidate-ladder builders (``stock_drift_fold`` /
``spot_key_candidates`` / ``spot_recovery_candidates``), consumed only
through the second facade ``brutex/data/lake_keys_rust.py`` — ASCII-only
by facade contract; computed-ASCII candidate strings cross OUT (the
window_ledger allowance, a conscious deviation from PR-1's integers-only
discipline).
"""

from __future__ import annotations

from brutex_data._brutex_data import (
    __version__,
    parse_fno_symbol_coords,
    spot_key_candidates,
    spot_recovery_candidates,
    stock_drift_fold,
)

__all__ = [
    "__version__",
    "parse_fno_symbol_coords",
    "spot_key_candidates",
    "spot_recovery_candidates",
    "stock_drift_fold",
]

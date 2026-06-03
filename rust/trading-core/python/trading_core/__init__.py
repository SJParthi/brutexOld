"""trading_core — shared indicator + 52-condition vocabulary kernel (PyO3 bridge).

Phase-2 scaffold for ``DEC-SHARED-CORE-001``. The compiled cdylib ships as the
private submodule ``trading_core._trading_core``; this ``__init__.py`` re-exports
its symbols so BruteX can write ``import trading_core; trading_core.evaluate_from_raw(...)``.

Phase 1 does NOT build the wheel (the crate is verified as a pure-Rust library
against the golden answer-key); importing this package requires the maturin build
that Phase 2 wires in.
"""

from __future__ import annotations

from ._trading_core import (  # type: ignore[import-not-found]
    TOTAL_CONDITIONS,
    __version__,
    evaluate_from_raw,
    total_conditions,
    version,
)

__all__ = [
    "TOTAL_CONDITIONS",
    "__version__",
    "evaluate_from_raw",
    "total_conditions",
    "version",
]

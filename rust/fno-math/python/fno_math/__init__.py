"""fno_math — BruteX Rust hot kernel (PyO3 bridge).

DEC-P0-055 (PR-N+2): mixed Python + Rust layout. The compiled cdylib
ships as the private submodule `fno_math._fno_math`; this `__init__.py`
re-exports its symbols so downstream callers continue to write
`import fno_math; fno_math.popcount_bytes(...)` unchanged.

The matched `__init__.pyi` + `py.typed` marker make every re-export
visible to `pyright --strict` and `mypy --strict` without per-call
`# pyright: ignore` shims (closes the PR-N revert post-mortem at
docs/MIGRATION.md #190).
"""

from __future__ import annotations

from ._fno_math import (
    ResidentBars,
    ResidentSweepSim,
    __version__,
    add_u64,
    bitand_count_u64s,
    bitcount_u64s,
    blake3_hash,
    count_supermask_u64s,
    count_supermasks_u64s,
    count_supermasks_u64s_nthreads,
    popcount_bytes,
)

__all__ = [
    "ResidentBars",
    "ResidentSweepSim",
    "__version__",
    "add_u64",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "count_supermask_u64s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "popcount_bytes",
]

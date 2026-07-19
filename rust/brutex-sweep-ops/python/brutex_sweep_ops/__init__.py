"""brutex_sweep_ops — sweep run-core/ops conversion crate (PyO3 bridge).

``DEC-RUST-SWEEP-OPS-001``: mixed Python + Rust layout (the ``brutex_feed`` /
``trading_core`` shape). The compiled cdylib ships as the private submodule
``brutex_sweep_ops._brutex_sweep_ops``; this ``__init__.py`` re-exports its
symbols so the consumer (``brutex/engine/sweep_ops_rust.py``) writes
``import brutex_sweep_ops; brutex_sweep_ops.join_path_segments(...)``.

Exposed surface (slice 1 — leaves):

* ``join_path_segments`` — the ``run_layout`` POSIX path-join law.
* ``gate_config_fragment`` — the ``sweep_identity`` gate-config fragment
  emission law.
* ``stall_scaled_timeout`` — the ``sweep_stall_policy`` scaled-timeout tail.
* ``RUN_DIR_SUBDIR`` — the Rust twin of ``run_layout.RUN_DIR_SUBDIR``
  (drift-pinned by the parity suite).

Slice 1b — the deferred leaf batch:

* ``progress_derived`` — the ``sweep_progress`` snapshot derived-metrics
  cluster (percent · elapsed · rate · ETA · survivor rate).
* ``sla_decision`` — the ``sweep_sla_guard`` projection decision.
* ``rank_plan_decision`` — the ``sweep_mem_budget`` rank-memory decision
  table.
* ``preflight_missing`` / ``freshness_stale`` — the two ``sweep_preflight``
  pure folds.
* ``liveness_tail`` — the ``sweep_liveness`` ``progress_verdict`` decision
  tail.

Wheel-source freshness handshake (the sibling-wheel pattern)
------------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s). The handshake
is deliberately UNCONDITIONAL — no env escape; the CONSUMER side is soft (the
default-OFF facade catches the ImportError and degrades to the byte-identical
Python oracles with a warn-once).

Later slices EXTEND ``_EXPECTED_COMPILED_SYMBOLS`` + the re-export block with
their own symbols (append-only — the ``python.rs`` registration law).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "RUN_DIR_SUBDIR",
    "__version__",
    "freshness_stale",
    "gate_config_fragment",
    "join_path_segments",
    "liveness_tail",
    "preflight_missing",
    "progress_derived",
    "rank_plan_decision",
    "sla_decision",
    "stall_scaled_timeout",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol."""
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "brutex_sweep_ops: the installed compiled wheel "
            f"(brutex_sweep_ops._brutex_sweep_ops at {location}) is STALE — it is "
            f"missing symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-sweep-ops "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_sweep_ops", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_sweep_ops import (  # noqa: E402
    RUN_DIR_SUBDIR,
    __version__,
    freshness_stale,
    gate_config_fragment,
    join_path_segments,
    liveness_tail,
    preflight_missing,
    progress_derived,
    rank_plan_decision,
    sla_decision,
    stall_scaled_timeout,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__``, NEVER hardcoded here.
__build_crate_version__: str = __version__

__all__ = [
    "RUN_DIR_SUBDIR",
    "__version__",
    "freshness_stale",
    "gate_config_fragment",
    "join_path_segments",
    "liveness_tail",
    "preflight_missing",
    "progress_derived",
    "rank_plan_decision",
    "sla_decision",
    "stall_scaled_timeout",
]

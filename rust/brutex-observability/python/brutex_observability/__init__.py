"""brutex_observability — observability decision-law kernel (PyO3 bridge).

``DEC-RUST-OBSERVABILITY-001``: mixed Python + Rust layout (the
``brutex_feed`` shape). The compiled cdylib ships as the private submodule
``brutex_observability._brutex_observability``; this ``__init__.py``
re-exports its symbols so the consumer facade
(``brutex/observability/observability_rust.py``) writes
``import brutex_observability; brutex_observability.sla_decide(...)``.

Exposed surface (SCALARS ONLY across every function — decide-in-Rust,
materialize-in-Python):

* ``loop_watchdog_check_once`` — law A, the tiered stall state machine
  (12-scalar verdict tuple).
* ``watchdog_decide`` — law B, the per-worker HEALTHY↔HUNG decision
  (``(elapsed, hung, edge)``).
* ``sla_decide`` — law C, the SLA predicate + hysteresis edge
  (``(breached, edge)``).
* ``stamp_shape_index`` — law D1, the strict-parse acceptance-order law
  (first-false index, ``-1`` accept).
* ``stamp_verdict`` — law D2, the validate verdict matrix
  (``"ok"`` / ``"hash_mismatch"``).
* ``tripwire_reconcile_counts`` — law T2, the count-reconciliation
  violation bitmask (DEC-RUST-OBSERVABILITY-TRIPWIRES-002; bit order = the
  ``_reconcile_counts`` append order).
* ``tripwire_throughput_watch`` — law T3, the throughput floor/dwell watch
  (``(new_peak, new_below, floor, code)``).
* ``tripwire_memory_budget`` — law T4a, the RSS budget arm
  (``(budget, code)``).
* ``tripwire_memory_slope`` — law T4b, the RSS growth-slope arm
  (``(slope, new_high, code)``).
* ``tripwire_summary_classify`` — law T5, the batch NaN/absurd screen
  (``(nan_codes, absurd_codes)``, aligned with the input order).
* ``tripwire_stage_missing`` — law T6, the stage-prerequisite membership
  mask over the pre-sorted required list.
* ``tripwire_ru_maxrss_bytes`` — law T9, the ``ru_maxrss`` platform unit
  law (``darwin`` → bytes verbatim, else KiB × 1024).
* ``PanicException`` — THIS wheel's own pyo3 panic class. pyo3's
  ``PanicException`` is a PER-EXTENSION-MODULE static (each abi3 wheel
  lazily inits its OWN type object), so a name-resolved class from
  ``pyo3_runtime`` (never a real importable module) or another extension
  (e.g. polars) can NEVER be this wheel's class — exporting our own lets
  the facade's panic belt resolve the EXACT class a panic from this wheel
  would raise (the measured IMPL-2 escape, closed structurally).
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s). The handshake
is deliberately UNCONDITIONAL — there is NO env escape; the only remedy for a
symbol-stale install is the rebuild the message names. The CONSUMER side is
nonetheless soft: the default-OFF facade catches the ImportError and latches
``crate_unavailable`` (a lacks-a-callable import latches ``crate_stale``),
serving the byte-identical Python oracle — a stale wheel can never fail a
monitor tick or a validate call.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped/added a symbol or
changed crate version. Same-version symbol-compatible BEHAVIORAL drift is
caught at runtime by the facade's per-law 16-sample burn-in bit-compare
(ship-the-oracle autorevert) — the first 16 engaged calls per law, not
forever; the doctor stage comparing the baked ``__source_digest__`` against
the source tree remains the named follow-up class.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors these names; a test pins the two
#: equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "PanicException",
    "SOURCE_DIGEST",
    "__version__",
    "loop_watchdog_check_once",
    "sla_decide",
    "stamp_shape_index",
    "stamp_verdict",
    "tripwire_memory_budget",
    "tripwire_memory_slope",
    "tripwire_reconcile_counts",
    "tripwire_ru_maxrss_bytes",
    "tripwire_stage_missing",
    "tripwire_summary_classify",
    "tripwire_throughput_watch",
    "watchdog_decide",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol.

    The teeth of the wheel-source freshness handshake: a symbol-incomplete
    (stale) compiled module fails HERE, at import, naming exactly what is
    missing and how to fix it — never a confusing ``ImportError: cannot
    import name ...`` deep inside a consumer.
    """
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "brutex_observability: the installed compiled wheel "
            f"(brutex_observability._brutex_observability at {location}) is STALE — "
            f"it is missing symbol(s) {', '.join(missing)} that this source tree's "
            "shim re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-observability "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_observability", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_observability import (  # noqa: E402
    SOURCE_DIGEST,
    PanicException,
    __version__,
    loop_watchdog_check_once,
    sla_decide,
    stamp_shape_index,
    stamp_verdict,
    tripwire_memory_budget,
    tripwire_memory_slope,
    tripwire_reconcile_counts,
    tripwire_ru_maxrss_bytes,
    tripwire_stage_missing,
    tripwire_summary_classify,
    tripwire_throughput_watch,
    watchdog_decide,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__`` (``env!("CARGO_PKG_VERSION")`` baked into
#: the ``.so`` at build time), NEVER hardcoded here. Deliberately NOT in
#: ``__all__`` (mirrors the siblings): a build stamp, not kernel API.
__build_crate_version__: str = __version__

#: Crate-source BLAKE3 digest of the INSTALLED compiled kernel (FRAMING v1;
#: DEC-RUST-SOURCE-DIGEST-TWIN-001). Same value as ``SOURCE_DIGEST`` — the
#: dunder spelling mirrors the sibling wheels' doctor-facing surface.
__source_digest__: str = SOURCE_DIGEST

__all__ = [
    "SOURCE_DIGEST",
    "PanicException",
    "__version__",
    "loop_watchdog_check_once",
    "sla_decide",
    "stamp_shape_index",
    "stamp_verdict",
    "tripwire_memory_budget",
    "tripwire_memory_slope",
    "tripwire_reconcile_counts",
    "tripwire_ru_maxrss_bytes",
    "tripwire_stage_missing",
    "tripwire_summary_classify",
    "tripwire_throughput_watch",
    "watchdog_decide",
]

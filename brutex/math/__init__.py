"""brutex.math — the single thin facade over the Rust hot-kernel wheels.

`CLAUDE.md` §5 documents this package as the *"facade to Rust hot kernel"*.
The two compiled PyO3 wheels are the single source of truth (Golden Rule
#11 COMMON):

* ``fno_math``     — ``rust/fno-math``     (popcount / supermask / resident
  bars + sweep-sim / BLAKE3 bit kernels).
* ``trading_core`` — ``rust/trading-core`` (the shared condition-vocabulary
  evaluator + pivot/fib indicator math, ``DEC-SHARED-CORE-001`` #441).

This package adds **NO logic** (Golden Rule #11 COMMON — no copy-paste, no
re-implementation). It imports both kernels and re-binds their public symbols
into one namespace so a caller can write a single, kernel-agnostic import::

    from brutex import math
    digest = math.blake3_hash(payload)        # fno_math
    mask = math.evaluate_with_levels(...)     # trading_core

The re-export is **programmatic** so the facade auto-tracks each kernel's
``__all__`` — when a kernel gains or drops an entry point the facade follows
with no edit here (and ``pyproject.toml`` already silences the static-analysis
noise this dynamic-``__all__`` rebind produces: ruff ``A005``/``PLE0604``,
pyright ``reportConstantRedefinition``/``reportUnsupportedDunderAll``).

Decision: ``DEC-STRUCTURE-CLEANUP-001`` (``docs/MIGRATION.md`` #569).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from types import ModuleType

# The Rust hot-kernel wheels this facade fronts, in re-export order.
_KERNEL_MODULES: tuple[str, ...] = ("fno_math", "trading_core")

# Each kernel ships its own ``__version__``; a single merged one would be
# ambiguous, so it is never re-bound (use :func:`kernel_versions` instead).
_VERSION_ATTR = "__version__"


def _public_names(module: ModuleType) -> list[str]:
    """Return a kernel module's public entry-point names.

    Uses the module's declared ``__all__`` when present (the compiled wheels
    publish one), else every non-underscore attribute. ``__version__`` is
    always filtered out — see :data:`_VERSION_ATTR`.

    Parameters
    ----------
    module:
        An imported Rust-kernel module.

    Returns
    -------
    list[str]
        Public names to re-export, excluding ``__version__``.
    """
    declared = getattr(module, "__all__", None)
    if declared is None:
        names = [name for name in vars(module) if not name.startswith("_")]
    else:
        names = list(declared)
    return [name for name in names if name != _VERSION_ATTR]


def _reexport(kernel_modules: Sequence[str], namespace: dict[str, object]) -> list[str]:
    """Import each kernel and re-bind its public symbols into ``namespace``.

    Parameters
    ----------
    kernel_modules:
        Importable module names of the Rust kernels to front.
    namespace:
        The target namespace (this package's ``globals()``) to bind into.

    Returns
    -------
    list[str]
        The re-exported names, sorted — suitable for ``__all__``.

    Raises
    ------
    RuntimeError
        If two kernels export the same symbol (an ambiguous facade is a
        correctness hazard, never silently resolved — Golden Rule #1).
    """
    exported: list[str] = []
    for kernel in kernel_modules:
        module = importlib.import_module(kernel)
        for name in _public_names(module):
            if name in namespace:
                raise RuntimeError(
                    f"brutex.math facade symbol collision: {name!r} is exported "
                    f"by more than one Rust kernel — refusing to shadow silently"
                )
            namespace[name] = getattr(module, name)
            exported.append(name)
    return sorted(exported)


def kernel_versions() -> dict[str, str]:
    """Map each fronted Rust kernel to its ``__version__`` string.

    The honest per-kernel provenance the facade deliberately does not merge
    into a single ambiguous ``__version__``.

    Returns
    -------
    dict[str, str]
        ``{kernel_name: version}`` for every kernel in :data:`_KERNEL_MODULES`.
    """
    versions: dict[str, str] = {}
    for kernel in _KERNEL_MODULES:
        module = importlib.import_module(kernel)
        versions[kernel] = str(getattr(module, _VERSION_ATTR))
    return versions


# Build the public surface programmatically: every kernel entry point, plus
# the facade's own provenance helper. ``__all__`` is intentionally rebound
# from a computed value (see the module docstring + pyproject silences).
__all__ = [*_reexport(_KERNEL_MODULES, globals()), "kernel_versions"]

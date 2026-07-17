"""brutex_strategies — combo run-identity byte-law kernels (PyO3 bridge).

``DEC-STRATEGIES-RUST-COMBO-001``: mixed Python + Rust layout (the
``brutex_feed`` / ``fno_math`` / ``trading_core`` shape). The compiled
cdylib ships as the private submodule
``brutex_strategies._brutex_strategies``; this ``__init__.py`` re-exports
its symbols so the consumer (``brutex/strategies/combo_rust.py``) writes
``import brutex_strategies; brutex_strategies.canonical_json(...)``.

Exposed surface:

* ``canonical_json`` — render the CPython canonical JSON of an
  ALREADY-lowered tree (dict / list / str / int / bool / None — float-free
  and Decimal-free BY LAW), byte-identical to ``json.dumps(tree,
  sort_keys=True, separators=(",", ":"), ensure_ascii=True)``. Extraction
  violations (out-of-i128 int, lone surrogate, str-subclass dict key,
  unsupported type) raise typed errors the facade degrades on — the oracle
  serves those calls byte-identically.
* ``first_unit_separator`` — index of the first VALUE containing ``\\x1f``
  (or ``None``); the decision half of ``combo.reject_unit_separator``.
  Python owns the field-name lookup, the error-message bytes and the
  injected exception class.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside the consumer. ``__build_crate_version__``
(derived from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``, never hardcoded) plus the in-repo ``EXPECTED_BUILD.json``
fingerprint are ready for a ``brutex doctor`` wheel-freshness stage (the
sibling crates' named-follow-up convention; this slice ships the stamp
surfaces only, never claims the stage).

The import-time handshake is deliberately UNCONDITIONAL — there is NO env
escape; the only remedy for a symbol-stale install is the rebuild the
ImportError message names. The CONSUMER side is nonetheless soft: the
default-OFF ``brutex/strategies/combo_rust.py`` resolver catches this
ImportError and degrades to the byte-identical Python oracle
(``crate_unavailable`` / ``crate_stale`` warn-once), so a stale wheel can
never fail a combo materialisation.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped or added a symbol or
changed crate version. It does NOT catch same-version, symbol-compatible
BEHAVIORAL drift at runtime beyond the facade's 16-call burn-in — the
merge-time catches for that class are the baked ``__source_digest__``
(FRAMING v1, DEC-RUST-SOURCE-DIGEST-TWIN-001), the cargo escaper matrix +
shared-literal cross-crate corpus, the frozen cross-language payload golden
and the wheel-ON hypothesis differential (design §4; the scan law's
detect-half and the escaper's non-ASCII branches get ZERO runtime burn-in
coverage — disclosed in the DEC's honest envelope, with wheel-ON
differential legs named as flip-DEC preconditions).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names; a test pins
#: the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "SOURCE_DIGEST",
    "__version__",
    "canonical_json",
    "first_unit_separator",
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
            "brutex_strategies: the installed compiled wheel "
            f"(brutex_strategies._brutex_strategies at {location}) is STALE — "
            f"it is missing symbol(s) {', '.join(missing)} that this source "
            "tree's shim re-exports. Rebuild + reinstall the wheel from the "
            "current source: run `maturin develop --release` inside "
            "rust/brutex-strategies (with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_strategies", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_strategies import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    canonical_json,
    first_unit_separator,
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
    "__version__",
    "canonical_json",
    "first_unit_separator",
]

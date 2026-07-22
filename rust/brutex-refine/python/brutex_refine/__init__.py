"""brutex_refine — the Stage-3 refinement optimizer core (PyO3 bridge).

``DEC-RUST-STAGE3-REFINE-001``: mixed Python + Rust layout (the
``brutex_feed`` / ``trading_core`` shape). The compiled cdylib ships as the
private submodule ``brutex_refine._brutex_refine``; this ``__init__.py``
re-exports its symbols so the consumer (``brutex/engine/refine_rust.py``)
writes ``import brutex_refine; brutex_refine.RefineSession(...)``.

Exposed surface:

* ``RefineSession`` — one mask's restarted-CMA-ES refinement (ask/tell
  protocol; ``ask()`` returns QUANTIZED on-grid ``(sl_paisa, tp_paisa)``
  int pairs, ``tell(values)`` reports the minimization objective — the
  Python wrapper negates Sharpe).
* ``SelectSession`` — the streaming bounded per-leaderboard top-K survivor
  selection (``push`` → ``(kept, evicted_seq)``, ``finish`` → sorted
  boards); the Python caller retains only the kept rows (the bounded-memory
  law of DEC-SWEEP-STREAMING-RANK-002).
* ``grid_validate`` / ``grid_denormalize`` / ``grid_normalize`` — the
  Mode-A [0,1] ↔ int-paisa bijection math (round-half-even snap; error
  messages byte-identical to the retired Python ``Stage3GridError`` text).
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md
  #1258); ALSO the refinement-run identity's ``optimizer_build`` token, so
  any crate-source change re-keys every refinement run (Golden Rule #4).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s). The
handshake is deliberately UNCONDITIONAL — no env escape; the only remedy is
the rebuild the message names. Unlike the default-OFF siblings the CONSUMER
side is HARD too: ``brutex/engine/refine_rust.py`` re-raises a typed
``RefineRustUnavailableError`` at CALL time (never at package import) — the
pycma oracle is DELETED, there is no fallback to degrade to.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped/added a symbol or
changed crate version. Same-version symbol-compatible BEHAVIORAL drift is
caught by the refinement-run identity instead — ``optimizer_build`` folds
the baked source digest, so a rebuilt-from-changed-source wheel mints NEW
refinement identities rather than silently reusing old ones; a stale-but-
symbol-complete wheel serves its OWN digest honestly. The ``brutex doctor``
freshness stage for this wheel is the named follow-up
``DEC-REFINE-RUST-DOCTOR-STAGE-001``.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names; a test pins
#: the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "RefineSession",
    "SOURCE_DIGEST",
    "SelectSession",
    "__version__",
    "grid_denormalize",
    "grid_normalize",
    "grid_validate",
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
            "brutex_refine: the installed compiled wheel "
            f"(brutex_refine._brutex_refine at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-refine "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_refine", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_refine import (  # noqa: E402
    SOURCE_DIGEST,
    RefineSession,
    SelectSession,
    __version__,
    grid_denormalize,
    grid_normalize,
    grid_validate,
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
    "RefineSession",
    "SelectSession",
    "__version__",
    "grid_denormalize",
    "grid_normalize",
    "grid_validate",
]

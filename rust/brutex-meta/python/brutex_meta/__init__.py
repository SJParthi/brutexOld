"""brutex_meta — meta-labeling + abstention-gate inference kernels (PyO3 bridge).

``DEC-META-LABEL-ANNOTATE-001`` + ``DEC-ABSTAIN-GATE-STREAM-001``: mixed
Python + Rust layout (the ``brutex_feed`` / ``trading_core`` shape). The
compiled cdylib ships as the private submodule ``brutex_meta._brutex_meta``;
this ``__init__.py`` re-exports its symbols so the consumer
(``brutex/meta/meta_rust.py``) writes ``import brutex_meta;
brutex_meta.score_logistic(...)``.

Exposed surface:

* ``score_logistic`` — the meta-label P(win) batch scorer: fixed-vector
  logistic inference over a flattened row-major feature matrix.
  O(features)/trade = O(1)/trade for the FIXED documented feature vector.
* ``abstain_masks`` — the abstention-first gate stream's per-element reason
  bitmasks (bit order = ``brutex/meta/abstain.py::ABSTAIN_REASON_ORDER``).
  O(1)/element.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside the consumer. The handshake is
deliberately UNCONDITIONAL — there is NO env escape; the only remedy for a
symbol-stale install is the rebuild the ImportError message names. The
CONSUMER side is nonetheless soft: the default-OFF ``brutex/meta/meta_rust.py``
resolver catches this ImportError and degrades to the byte-comparable Python
oracle (``wheel_missing`` / ``wheel_stale`` warn-once), so a stale wheel can
never fail an annotation pass.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped or added a symbol or
changed crate version. It does NOT catch same-version, symbol-compatible
BEHAVIORAL drift — the runtime catch for that class is the facade's 16-call
burn-in bit-compare against the retained Python oracle with
ship-the-oracle autorevert (``brutex/meta/meta_rust.py`` — shipped from day
one, the lake-facade posture), plus the refuse-poison output validation.
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
    "abstain_masks",
    "score_logistic",
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
            "brutex_meta: the installed compiled wheel "
            f"(brutex_meta._brutex_meta at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-meta "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_meta", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_meta import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    abstain_masks,
    score_logistic,
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
    "abstain_masks",
    "score_logistic",
]

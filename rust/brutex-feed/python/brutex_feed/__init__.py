"""brutex_feed — engine read-boundary decision kernel (PyO3 bridge).

``DEC-FEED-RUST-CORE-001``: mixed Python + Rust layout (the ``fno_math`` /
``trading_core`` shape). The compiled cdylib ships as the private submodule
``brutex_feed._brutex_feed``; this ``__init__.py`` re-exports its symbols so
the consumer (``brutex/feed_rust.py``) writes ``import brutex_feed;
brutex_feed.scan_bars(...)``.

Exposed surface:

* ``scan_bars`` — the ONE pure decision function: positivity refusal order +
  bad-tick keep/skip + owed-WARN coordinates over raw f64/i64 bar columns,
  returning INTEGERS ONLY (kept row indices, warn-event coordinates, the
  positivity-stop coordinate) or ``None`` (abstain — the caller degrades to
  the retained Python oracle). No float/string/timestamp ever crosses
  Rust → Python output.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the fno_math/trading_core pattern)
--------------------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside the consumer. ``__build_crate_version__``
(derived from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``, never hardcoded) plus the in-repo ``EXPECTED_BUILD.json``
fingerprint are ready for a ``brutex doctor`` wheel-freshness stage — the
named follow-up ``DEC-FEED-RUST-DOCTOR-STAGE-001`` (this slice ships the
stamp surfaces only, never claims the stage).

The import-time handshake is deliberately UNCONDITIONAL — there is NO env
escape; the only remedy for a symbol-stale install is the rebuild the
ImportError message names. The CONSUMER side is nonetheless soft: the
default-OFF ``brutex/feed_rust.py`` resolver catches this ImportError and
degrades to the byte-identical Python oracle (``wheel_missing`` /
``wheel_stale`` warn-once), so a stale wheel can never fail a read.

HONEST residual gap (Golden Rule #10, same envelope as the siblings'): the
version + symbol-set handshake catches a stale wheel that dropped or added a
symbol or changed crate version. It does NOT catch same-version,
symbol-compatible BEHAVIORAL drift — and neither does anything else at
runtime in this slice: the ``validate_scan`` gate in ``brutex/feed_rust.py``
is STRUCTURAL-only (shape / ordering / range laws), so a same-version,
symbol-complete wheel whose DECISIONS drifted while keeping a valid shape
serves silently. The catches for that class are the named follow-ups — the
``brutex doctor`` stage comparing the baked ``__source_digest__`` against
the source tree (``DEC-FEED-RUST-DOCTOR-STAGE-001``) and the flip-time
burn-in self-check (``DEC-FEED-RUST-FLIP-001``); this slice ships the stamp
surfaces only, never claims either.
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
    "scan_bars",
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
            "brutex_feed: the installed compiled wheel "
            f"(brutex_feed._brutex_feed at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-feed "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_feed", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_feed import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    scan_bars,
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
    "scan_bars",
]

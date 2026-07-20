"""brutex_notify — notification pure-law kernel (PyO3 bridge).

``DEC-NOTIFY-RUST-CORE-001``: mixed Python + Rust layout (the ``brutex_feed``
/ ``trading_core`` shape). The compiled cdylib ships as the private submodule
``brutex_notify._brutex_notify``; this ``__init__.py`` re-exports its symbols
so the consumer (``brutex/notifications/notify_rust.py``) writes
``import brutex_notify; brutex_notify.escape_markdown_v2(...)``.

Exposed surface (the four PURE notification laws — nothing else):

* ``escape_markdown_v2`` — the MarkdownV2 escape law (per-code-point scan,
  the 18-char reserved set, the backslash deliberately NOT self-escaped).
* ``count_or_question`` — ``"?"`` on ``None`` (the honest unknown — never a
  fabricated 0), else the plain decimal rendering.
* ``dedupe_should_drop`` — the dedupe-window drop predicate over IEEE-754
  f64 BIT PATTERNS carried as ``int`` (no float object crosses the FFI).
* ``truncate_id_12`` — the ``text[:12]`` CODE-POINT truncation law.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

No secret (bot token / chat id / config) ever crosses this surface — message
TEXT and integer bit patterns only.

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside the consumer. ``__build_crate_version__``
(derived from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``, never hardcoded) plus the in-repo ``EXPECTED_BUILD.json``
fingerprint are ready for a ``brutex doctor`` wheel-freshness stage — a
named later lane (this slice ships the stamp surfaces only).

The import-time handshake is deliberately UNCONDITIONAL — there is NO env
escape; the only remedy for a symbol-stale install is the rebuild the
ImportError message names. The CONSUMER side is nonetheless soft: the
default-OFF ``brutex/notifications/notify_rust.py`` facade catches this
ImportError and degrades to the byte-identical Python oracle
(``crate_unavailable`` / ``crate_stale`` warn-once), so a stale wheel can
never fail — or even slow — an alert emit.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped or added a symbol or
changed crate version. It does NOT catch same-version, symbol-compatible
BEHAVIORAL drift — the runtime catch for that class is the facade's
per-process 16-call burn-in bit-compare per law (any mismatch ships the
ORACLE value + autoreverts the Rust path), which bounds the blast radius but
is not a proof; the doctor digest stage remains the named later lane.
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
    "count_or_question",
    "dedupe_should_drop",
    "escape_markdown_v2",
    "truncate_id_12",
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
            "brutex_notify: the installed compiled wheel "
            f"(brutex_notify._brutex_notify at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-notify "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_notify", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_notify import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    count_or_question,
    dedupe_should_drop,
    escape_markdown_v2,
    truncate_id_12,
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
    "count_or_question",
    "dedupe_should_drop",
    "escape_markdown_v2",
    "truncate_id_12",
]

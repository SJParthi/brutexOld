"""brutex_ops — BruteX ops-lane Rust kernels (PyO3 bridge).

DEC-CROSSVERIFY-RUST-CORE (first cut). The compiled cdylib ships as the
private submodule ``brutex_ops._brutex_ops``; this ``__init__.py`` re-exports
its symbols (the fno-math DEC-P0-055 mixed layout) so the consumer — the
default-OFF facade :mod:`brutex.crossverify.rust_seam` — writes
``import brutex_ops; brutex_ops.crossverify_render_csv(...)``.

Exposed surface (the ``crossverify`` lane; future ops lanes — backup,
nightly, … — APPEND their own re-export lines, never editing this block):

* ``crossverify_render_csv`` — the pinned CSV byte assembly over
  pre-validated, pre-formatted cell rows; returns ``(bytes, None)`` or
  ``(None, (code, row, cell))`` (the typed string-law refusal belt).
* ``crossverify_canonical_identity`` — the deny-by-default lake-coordinate →
  canonical-identity mapping over Python-parsed FNO grammar fields.
* ``crossverify_s3_key`` — the ONE S3 key assembly law.
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Freshness posture (the brutex-lake shape, DELIBERATELY not the
brutex-feed ``_EXPECTED_COMPILED_SYMBOLS`` import-time handshake): the
API-surface guard lives in the CONSUMING facade
(:data:`brutex.crossverify.rust_seam._CRATE_API` — an absent/stale symbol
latches ``crate_unavailable``/``crate_stale`` with one loud WARN and the
Python oracles serve), so a stale wheel can never break a cross-verify run.
The ``EXPECTED_BUILD.json`` handshake belongs to trading-core/fno-math (the
#1912 ruling) and is deliberately absent here — the burn-in bit-compare in
the facade is the behavioral-drift tooth.
"""

from __future__ import annotations

from ._brutex_ops import (
    SOURCE_DIGEST,
    __version__,
    crossverify_canonical_identity,
    crossverify_render_csv,
    crossverify_s3_key,
    version,
)

#: Crate-source BLAKE3 digest of the INSTALLED compiled kernel (FRAMING v1;
#: DEC-RUST-SOURCE-DIGEST-TWIN-001). Same value as ``SOURCE_DIGEST`` — the
#: dunder spelling mirrors the sibling wheels' doctor-facing surface.
__source_digest__: str = SOURCE_DIGEST

__all__ = [
    "SOURCE_DIGEST",
    "__version__",
    "crossverify_canonical_identity",
    "crossverify_render_csv",
    "crossverify_s3_key",
    "version",
]

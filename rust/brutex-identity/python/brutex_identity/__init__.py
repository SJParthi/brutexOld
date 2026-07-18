"""brutex_identity — run-identity + ledger-store kernels (PyO3 bridge).

``DEC-RUST-RUNID-STORES-001``: mixed Python + Rust layout (the ``fno_math`` /
``trading_core`` / ``brutex_feed`` shape). The compiled cdylib ships as the
private submodule ``brutex_identity._brutex_identity``; this ``__init__.py``
re-exports its symbols so the consumers (``brutex/strategies/combo.py``,
``brutex/engine/snapshot_hash.py``, ``brutex/engine/results_store.py``,
``brutex/data/window_ledger_store.py``) write ``import brutex_identity; …``.

Exposed surface:

* ``canonicalise_params_json`` / ``combo_hash`` / ``find_unit_separator_field``
  — the 9-field BLAKE3 run-identity join + the full param canonicaliser
  (CPython-repr floats, CPython-``json.dumps`` byte-law, the ``\\x1f`` screen);
* ``SnapshotHasher`` — the streamed ``bxsnap2`` data-snapshot digest;
* ``classify_result_append`` / ``validate_run_id_error`` / ``mask_to_limbs`` /
  ``mask_from_limbs`` / ``rank_top_k`` — the results-store laws;
* ``merge_ledger_rows`` / ``render_ledger_parquet`` / ``read_ledger_rows`` /
  ``LedgerStringPool`` / ``LEDGER_COLUMNS`` / ``LEDGER_CORE_COLUMNS`` — the
  window-ledger store laws + the canonical Parquet codec (the #1917 G10
  resolution, option (iii));
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the fno_math/trading_core pattern)
--------------------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside a consumer. Deliberately UNCONDITIONAL —
no env escape; the only remedy for a symbol-stale install is the rebuild the
message names. Unlike the default-OFF sibling seams, the consumers of THIS
wheel import it HARD (the fno_math posture): the Python computation bodies
were deleted in the same lane, so there is no oracle to degrade to.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped/added a symbol or
changed crate version. It does NOT catch same-version, symbol-compatible
BEHAVIORAL drift at import time — the frozen pre-conversion oracle fixture
(``tests/golden/test_runid_stores_oracle_golden.py``) is the drift net, run
per push in CI, not at import.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names; a test pins
#: the two equal (the brutex_feed convention).
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "LEDGER_COLUMNS",
    "LEDGER_CORE_COLUMNS",
    "LedgerStringPool",
    "SOURCE_DIGEST",
    "SnapshotHasher",
    "__version__",
    "canonicalise_params_json",
    "classify_result_append",
    "combo_hash",
    "derive_empty_streak",
    "derive_in_pull_fetches",
    "find_unit_separator_field",
    "mask_from_limbs",
    "mask_to_limbs",
    "merge_ledger_rows",
    "rank_top_k",
    "read_ledger_rows",
    "render_ledger_parquet",
    "validate_run_id_error",
    "version",
)

_compiled = importlib.import_module("brutex_identity._brutex_identity")

_missing: Sequence[str] = tuple(
    name for name in _EXPECTED_COMPILED_SYMBOLS if not hasattr(_compiled, name)
)
if _missing:
    raise ImportError(
        "brutex_identity: the installed compiled wheel is STALE — it lacks "
        f"{', '.join(_missing)}. Rebuild it from the current source tree: "
        "pip install ./rust/brutex-identity  (or: cd rust/brutex-identity && "
        "maturin develop --release)"
    )

LEDGER_COLUMNS = _compiled.LEDGER_COLUMNS
LEDGER_CORE_COLUMNS = _compiled.LEDGER_CORE_COLUMNS
LedgerStringPool = _compiled.LedgerStringPool
SOURCE_DIGEST = _compiled.SOURCE_DIGEST
SnapshotHasher = _compiled.SnapshotHasher
canonicalise_params_json = _compiled.canonicalise_params_json
classify_result_append = _compiled.classify_result_append
combo_hash = _compiled.combo_hash
derive_empty_streak = _compiled.derive_empty_streak
derive_in_pull_fetches = _compiled.derive_in_pull_fetches
find_unit_separator_field = _compiled.find_unit_separator_field
mask_from_limbs = _compiled.mask_from_limbs
mask_to_limbs = _compiled.mask_to_limbs
merge_ledger_rows = _compiled.merge_ledger_rows
rank_top_k = _compiled.rank_top_k
read_ledger_rows = _compiled.read_ledger_rows
render_ledger_parquet = _compiled.render_ledger_parquet
validate_run_id_error = _compiled.validate_run_id_error
version = _compiled.version

__version__: str = str(_compiled.__version__)
#: The crate version the compiled module was built from (`CARGO_PKG_VERSION`
#: baked at build time — never hardcoded here).
__build_crate_version__: str = __version__
__source_digest__: str = str(_compiled.__source_digest__)

__all__ = [
    "LEDGER_COLUMNS",
    "LEDGER_CORE_COLUMNS",
    "SOURCE_DIGEST",
    "LedgerStringPool",
    "SnapshotHasher",
    "canonicalise_params_json",
    "classify_result_append",
    "combo_hash",
    "derive_empty_streak",
    "derive_in_pull_fetches",
    "find_unit_separator_field",
    "mask_from_limbs",
    "mask_to_limbs",
    "merge_ledger_rows",
    "rank_top_k",
    "read_ledger_rows",
    "render_ledger_parquet",
    "validate_run_id_error",
    "version",
]

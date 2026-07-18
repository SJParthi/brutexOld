"""Canonical combo identity hashing — FR-BF-003 + DEC-P0-060 (9-field, BLAKE3).

Every combo in a brute-force sweep is uniquely identified by the
9-field BLAKE3 digest mandated at ``docs/TRACK1_BACKTEST_SPEC.md``
§"Identity"::

    combo_hash = blake3(
        strategy_id ‖ direction ‖ instrument ‖ tf ‖ mode ‖
        canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
        code_commit_sha
    )

History
-------

* **PR #25 era — 4-field SHA256.** Original implementation hashed
  ``(strategy_id, params, data_snapshot_hash, code_commit_sha)``.
* **DEC-P0-044 (MIGRATION.md #181, 2026-05-18).** Documented the gap
  against the spec (missing 5 inputs + SHA256-vs-BLAKE3 mismatch);
  deferred to the engine PR to amortise the test-pin update cost.
* **DEC-P0-052 (MIGRATION.md #189, PR-M).** Shipped the
  ``fno_math.blake3_hash`` Rust kernel as the foundational primitive.
* **DEC-P0-060.** Landed the full 9-field BLAKE3 migration.
* **DEC-RUST-RUNID-STORES-001 (the L14 conversion lane).** The WHOLE
  computation — the recursive param canonicaliser (CPython-``repr`` floats,
  the CPython-``json.dumps`` sorted/compact/``ensure_ascii`` byte-law), the
  ``\\x1f`` unit-separator screen and the 9-field BLAKE3 join — now runs in
  the ``brutex_identity`` kernel (``rust/brutex-identity``); this module is
  the thin typed wrapper owning the :class:`StrategyError` contract.
  Byte-parity is proven by the FROZEN pre-conversion oracle
  (``tests/golden/fixtures/runid_stores_oracle_v1.json`` — captured from the
  last all-Python implementation) + the live CPython differential suite, so
  every existing run identity is unchanged (Golden Rule #4).

Why BLAKE3:

* ~5× faster than SHA256 on single core; ~10-20× on multi-core for
  large inputs (BLAKE3 paper §5.5).
* 32-byte (256-bit) digest matches SHA256 — wire-format-compatible
  at the LMDB key + Bloom hash layers (``COMBO_HASH_HEX_LEN = 64``
  unchanged).
* Single-tree mode is deterministic + reproducible (Golden Rule #4).

The canonicalisation rules (NFR-UNIQ-002) are:

* Keys sorted lexicographically.
* Floats serialised via ``repr()`` so ``1.0`` and ``1.00`` hash the
  same but ``1.0 + 1e-16`` hashes differently (preserving meaningful
  precision).
* Non-finite floats AND Decimals (NaN / ±inf / ±Infinity) are rejected
  — propagating them through a hash is undefined (``NaN != NaN``; an
  infinity has no exact textual round-trip via ``repr``).
* Nested dicts / lists / tuples are recursed into; tuples are
  treated as ordered sequences.
* ``Decimal`` values are serialised via ``str()`` so their exact
  textual form round-trips through the hash.

Honest envelope — wire-locked properties (do not "fix")
-------------------------------------------------------

These behaviours are FROZEN by the golden corpus and bit-mirrored by
the engine canonicaliser; "fixing" any of them is a wire-format break:

1. A ``float``, ``str``, and ``Decimal`` carrying the same text
   canonicalise to the same token (``1.5`` ≡ ``"1.5"`` ≡
   ``Decimal("1.5")``). Mitigation: sweep callers use ONE type per
   param axis — :class:`~brutex.strategies.sweep.ParamGrid` is the
   choke point, so a single axis never mixes float / str / Decimal.
2. ``Decimal`` scale is significant (``Decimal("1.0") !=
   Decimal("1.00")``) — asymmetric with float's repr scale-collapse
   (``1.0`` ≡ ``1.00``).
3. Signed zero is NORMALISED (``-0.0`` -> ``+0.0`` before ``repr``, so
   both canonicalise to ``"0.0"``) — two mathematically-equal params
   hash to the SAME run identity (the results_store / trial_stats
   precedent).
4. ``tuple`` / ``list`` / ``range`` all canonicalise to a JSON array.

Disclosed narrowing (DEC-RUST-RUNID-STORES-001): a ``str`` carrying a
LONE SURROGATE (illegal Unicode, producible only via ``surrogateescape``)
is now REJECTED with a typed :class:`StrategyError` — the retired Python
body hashed the raw code point; no engine caller can produce one.

This module imports ``brutex_identity`` (the BruteX run-identity Rust
kernel wheel, shipped with bundled ``py.typed`` stubs) — no other heavy
third-party dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

import brutex_identity

from brutex.strategies.errors import StrategyError

#: Maximum nesting depth accepted by the canonicaliser. Far above any
#: realistic param (production params are flat dicts of scalars, occasionally a
#: one-level list). A deeper structure raises :class:`StrategyError` rather than
#: crashing with ``RecursionError``. The VALUE is enforced inside the
#: ``brutex_identity`` kernel (its ``MAX_DEPTH`` twin — drift-pinned by the
#: boundary test at exactly this constant); this Python constant is the
#: documented import surface. The engine run-identity mirrors
#: (``brutex/engine/{stage1a,sweep_driver,stage3_refine}.py``) adopted guard
#: parity via :func:`reject_unit_separator` + ``allow_nan=False``
#: (DEC-RUN-IDENTITY-GUARD-PARITY-001); this depth cap itself is structurally
#: vacuous at those sites — their payloads are flat scalars, no recursion
#: exists to cap.
_MAX_CANONICALISE_DEPTH: int = 64

#: The identity-payload field-join delimiter. Every run-identity join
#: (:func:`combo_hash` + the engine mirrors above) separates raw string fields
#: with this byte, so a field CARRYING it could smuggle a field boundary and
#: forge another identity's payload — :func:`reject_unit_separator` is the ONE
#: COMMON reject-only guard (CLAUDE.md §11 COMMON).
UNIT_SEPARATOR: Final[str] = "\x1f"


def reject_unit_separator(
    context: str,
    fields: Iterable[tuple[str, str]],
    *,
    error: type[Exception],
) -> None:
    """Reject any ``(name, value)`` field whose value contains ``\\x1f``.

    The ONE COMMON guard for every ``\\x1f``-joined run-identity payload
    (DEC-RUN-IDENTITY-GUARD-PARITY-001): a raw field carrying the join
    delimiter could shift a field boundary and forge a different identity, so
    it is rejected BEFORE any digest byte is produced. REJECT-ONLY — no
    currently-legal input's digest changes. The scan itself runs in the
    ``brutex_identity`` kernel (:func:`brutex_identity.find_unit_separator_field`
    — first offending field name, or ``None``); this wrapper owns the
    caller-injected error contract.

    Parameters
    ----------
    context
        The calling identity function's name, prefixed onto the error message
        (``"combo_hash"`` reproduces the historical message byte-for-byte).
    fields
        ``(field_name, field_value)`` pairs to screen, in order.
    error
        The caller's domain error class to raise (:class:`StrategyError` for
        :func:`combo_hash`, ``Stage3RefineError`` / ``ValueError`` at the
        engine mirrors — each site keeps its own error contract).

    Raises
    ------
    Exception
        The injected *error* class, naming the offending field.
    """
    offender = brutex_identity.find_unit_separator_field(list(fields))
    if offender is not None:
        raise error(
            f"{context}: {offender} must not contain the \\x1f "
            "unit-separator byte (it is the field join delimiter)"
        )


def canonicalise_params(params: Mapping[str, Any]) -> str:
    """Return a deterministic JSON-string encoding of *params*.

    This is the string that gets fed into the BLAKE3 hasher underlying
    :func:`combo_hash`. Exposed on its own so tests can assert on the
    canonical form directly without going through the hasher. The encoding
    runs entirely in the ``brutex_identity`` kernel
    (:func:`brutex_identity.canonicalise_params_json` — one pass emitting
    the exact ``json.dumps(sort_keys=True, separators=(",", ":"),
    ensure_ascii=True)`` bytes of the retired Python body, floats via the
    CPython-``repr`` twin); this wrapper maps the kernel's typed rejection
    onto the :class:`StrategyError` contract.

    Raises
    ------
    StrategyError
        If *params* is not a :class:`~collections.abc.Mapping`, contains
        a non-finite float / Decimal, a non-string dict key, a lone-surrogate
        string (the disclosed narrowing — see the module docstring), or a
        value type the encoder can't represent (sets, bytes, custom
        objects, …).
    """
    try:
        return str(brutex_identity.canonicalise_params_json(params))
    except ValueError as exc:
        raise StrategyError(str(exc)) from exc


def combo_hash(
    *,
    strategy_id: str,
    direction: str,
    instrument: str,
    tf: str,
    mode: str,
    params: Mapping[str, Any],
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> str:
    """Return the 64-char BLAKE3 hex digest of a combo's 9-field identity.

    Per FR-BF-003 / NFR-UNIQ-001 and the
    ``docs/TRACK1_BACKTEST_SPEC.md`` §"Identity" mandate, two combos
    collide iff all nine inputs collide. The :func:`canonicalise_params`
    step guarantees dict-key ordering does not affect the result. The
    whole derivation — the non-empty screen, the ``\\x1f`` screen, the
    canonical-params encoding, the 9-field join and the BLAKE3 digest —
    runs in the ``brutex_identity`` kernel
    (:func:`brutex_identity.combo_hash`); this wrapper owns the
    :class:`StrategyError` contract (message bytes unchanged).

    Parameters
    ----------
    strategy_id
        Unique identifier from :attr:`StrategyMeta.strategy_id`.
    direction
        Trade direction (``"bull"``, ``"bear"``, ``"neutral"``). The
        engine PR will pass real values; pre-engine callers pass
        ``"N/A"`` placeholders with ``# TODO`` markers.
    instrument
        Instrument symbol (``"NIFTY"``, ``"BANKNIFTY"``, ``"SENSEX"``,
        ``"INDIA VIX"`` per CLAUDE.md §6).
    tf
        Timeframe label (one of the 21 TFs per
        ``brutex/data/timeframes.py``).
    mode
        Exit-mode label (``"A"`` = SL/TP-based, ``"B"`` = signal-exit
        per MIGRATION.md decisions #22-#23).
    params
        Concrete parameter dict for this combo. Canonicalised via
        :func:`canonicalise_params`.
    data_snapshot_hash
        BLAKE3 hex of the data slice the combo will run against.
        Typically produced by
        :meth:`~brutex.data.catalog.Catalog.data_snapshot_hash`
        (FR-CAT-004).
    indicator_version
        Version tag of the 9-indicator computation stack. Bumping this
        invalidates every previously-cached combo, forcing
        re-evaluation against the new indicator outputs.
    code_commit_sha
        Git commit SHA of the BruteX checkout running the combo.
        Included so a bug-fix release does not return a stale cached
        result.

    Raises
    ------
    StrategyError
        If any required string input is empty, or any raw field
        contains the ``\x1f`` unit-separator byte used to join the
        nine fields, or *params* fails canonicalisation.

    Complexity
    ----------
    O(P) in the serialised payload size (BLAKE3 + the canonical encoding are
    linear; the canonicaliser visits each param node once). O(1) per combo
    with respect to sweep cardinality N — called exactly once per combo, cost
    independent of total combo count (the SCALABLE / O(1)-per-write property
    of Golden Rule #5).
    """
    try:
        return str(
            brutex_identity.combo_hash(
                strategy_id,
                direction,
                instrument,
                tf,
                mode,
                params,
                data_snapshot_hash,
                indicator_version,
                code_commit_sha,
            )
        )
    except ValueError as exc:
        raise StrategyError(str(exc)) from exc

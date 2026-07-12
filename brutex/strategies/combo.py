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
* **DEC-P0-060 (this module, PR-N+7).** Lands the full 9-field BLAKE3
  migration ahead of the engine PR. The 5 new fields are accepted as
  keyword-only required parameters; callers that don't yet have real
  values pass placeholder strings (``"N/A"``) with explicit
  ``# TODO(engine PR)`` markers — see ``brutex/strategies/sweep.py``.

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
   precedent; see the float branch of :func:`_canonicalise_value`).
4. ``tuple`` / ``list`` / ``range`` all canonicalise to a JSON array.

This module imports ``fno_math`` (the BruteX Rust hot-kernel wheel,
shipped via DEC-P0-055 with bundled ``py.typed`` stubs) — no other
heavy third-party dependency.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

import fno_math

from brutex.strategies.errors import StrategyError

#: Maximum nesting depth accepted by :func:`_canonicalise_value`. Far above any
#: realistic param (production params are flat dicts of scalars, occasionally a
#: one-level list). A deeper structure raises :class:`StrategyError` rather than
#: crashing with ``RecursionError``. The engine run-identity mirrors
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
    currently-legal input's digest changes.

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
    for field_name, field_value in fields:
        if UNIT_SEPARATOR in field_value:
            raise error(
                f"{context}: {field_name} must not contain the \\x1f "
                "unit-separator byte (it is the field join delimiter)"
            )


def canonicalise_params(params: Mapping[str, Any]) -> str:
    """Return a deterministic JSON-string encoding of *params*.

    This is the string that gets fed into the BLAKE3 hasher underlying
    :func:`combo_hash`. Exposed on its own so tests can assert on the
    canonical form directly without going through the hasher.

    Raises
    ------
    StrategyError
        If *params* is not a :class:`~collections.abc.Mapping`, contains
        a non-finite float / Decimal, a non-string dict key, or a value
        type the encoder can't represent (sets, bytes, custom objects, …).
    """
    if not isinstance(params, Mapping):
        raise StrategyError(
            f"canonicalise_params: top-level params must be a Mapping, got {type(params).__name__}"
        )
    return json.dumps(
        _canonicalise_value(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


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
    step guarantees dict-key ordering does not affect the result.

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
        nine fields.

    Complexity
    ----------
    O(P) in the serialised payload size (BLAKE3 + ``json.dumps`` are linear;
    the canonicaliser visits each param node once). O(1) per combo with
    respect to sweep cardinality N — called exactly once per combo, cost
    independent of total combo count (the SCALABLE / O(1)-per-write property
    of Golden Rule #5).
    """
    if not strategy_id:
        raise StrategyError("combo_hash: strategy_id must be non-empty")
    if not direction:
        raise StrategyError("combo_hash: direction must be non-empty")
    if not instrument:
        raise StrategyError("combo_hash: instrument must be non-empty")
    if not tf:
        raise StrategyError("combo_hash: tf must be non-empty")
    if not mode:
        raise StrategyError("combo_hash: mode must be non-empty")
    if not data_snapshot_hash:
        raise StrategyError("combo_hash: data_snapshot_hash must be non-empty")
    if not indicator_version:
        raise StrategyError("combo_hash: indicator_version must be non-empty")
    if not code_commit_sha:
        raise StrategyError("combo_hash: code_commit_sha must be non-empty")

    reject_unit_separator(
        "combo_hash",
        (
            ("strategy_id", strategy_id),
            ("direction", direction),
            ("instrument", instrument),
            ("tf", tf),
            ("mode", mode),
            ("data_snapshot_hash", data_snapshot_hash),
            ("indicator_version", indicator_version),
            ("code_commit_sha", code_commit_sha),
        ),
        error=StrategyError,
    )

    canonical = canonicalise_params(params)
    payload = UNIT_SEPARATOR.join(
        [
            strategy_id,
            direction,
            instrument,
            tf,
            mode,
            canonical,
            data_snapshot_hash,
            indicator_version,
            code_commit_sha,
        ]
    )
    # str(...) keeps this typed `str` even where the fno_math wheel (and its
    # stubs) is absent and the import degrades to Any — e.g. the isolated
    # pre-commit mypy env (hook-portability, DEC-OPTIONS-ELIGIBLE-PRECISE-001).
    return str(fno_math.blake3_hash(payload.encode("utf-8")).hex())


# ────────────────────────────────────────────────────────────────────────
# Private helpers
# ────────────────────────────────────────────────────────────────────────


def _canonicalise_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursive one-step canonicalisation used by :func:`canonicalise_params`.

    Returns a value the stdlib ``json`` module can serialise directly.

    A non-finite float / Decimal (NaN / ±inf / ±Infinity), a ``float``
    subclass (e.g. ``numpy.float64``), and nesting deeper than
    :data:`_MAX_CANONICALISE_DEPTH` levels are each rejected with
    :class:`StrategyError` rather than silently corrupting the digest or
    crashing with ``RecursionError``.
    """
    if _depth > _MAX_CANONICALISE_DEPTH:
        raise StrategyError(
            f"canonicalise_params: nesting deeper than {_MAX_CANONICALISE_DEPTH} "
            "levels is not supported"
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if type(value) is not float:
            raise StrategyError(
                "canonicalise_params: float subclasses (e.g. numpy.float64) are "
                f"not accepted — pass a built-in float; got {type(value).__name__}"
            )
        if not math.isfinite(value):
            raise StrategyError("canonicalise_params: non-finite floats (NaN/inf) are not hashable")
        # Canonicalise signed zero (-0.0 → +0.0) BEFORE repr so two
        # mathematically-equal params hash to the SAME run-identity (Golden
        # Rule #4 reproducibility) — repr(-0.0) is "-0.0" but repr(0.0) is
        # "0.0", which would otherwise split one run into two. `value + 0.0` is
        # exact (bit-unchanged) for every other finite double; only the sign of
        # zero is normalised (the results_store / trial_stats precedent).
        value = value + 0.0
        # repr(float) round-trips exactly in CPython — two floats with
        # the same bit pattern produce the same repr string, so this
        # is the safest stable form.
        return repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise StrategyError(
                "canonicalise_params: non-finite Decimals (NaN/Infinity) are not hashable"
            )
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise StrategyError(
                    f"canonicalise_params: dict keys must be str, got {type(k).__name__}"
                )
            out[k] = _canonicalise_value(v, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_canonicalise_value(v, _depth=_depth + 1) for v in value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_canonicalise_value(v, _depth=_depth + 1) for v in value]
    raise StrategyError(
        f"canonicalise_params: unsupported value type {type(value).__name__}: {value!r}"
    )

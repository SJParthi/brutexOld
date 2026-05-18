"""Canonical parameter hashing for ``FR-BF-003`` combo identity.

Every combo in a brute-force sweep is uniquely identified by::

    combo_hash = sha256(
        strategy_id ‖ canonical_params ‖ data_snapshot_hash ‖ code_commit_sha
    )

.. warning::

    **DEC-P0-044 known gap (closing in Phase 3 engine PR):** the spec at
    `docs/TRACK1_BACKTEST_SPEC.md` §"Identity" mandates 9 hash inputs:

        ``strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖
        canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
        code_commit_hash``

    This module currently hashes 4 of the 9 (`strategy_id` ≈
    `strategy_mask`, `canonical_params`, `data_snapshot_hash`,
    `code_commit_sha`). **Missing: direction, instrument, tf, mode,
    indicator_version.** Per `docs/TRACK1_REQUIREMENTS_FINAL.md` C-04
    + DEC-P0-040 Q-02 lock, the migration is **deferred to PR-H
    (engine bloc)** because:

    1. The missing 5 inputs are produced by Stage 1A/1B/2/3/4 engine
       modules that don't yet exist (R-106 GAP).
    2. Migrating sha256 → blake3 (DEC-P0-040 Q-02) requires
       regenerating every pinned hash in the test suite — coupling
       this to the engine PR amortises the test-pin update cost.
    3. Adding the 5 fields without callers that produce them creates
       a dead-code surface that complicates the next PR's review.

    Until then, callers MUST embed direction/instrument/tf/mode/
    indicator_version inside the ``params`` dict if they want them
    to participate in the hash. This is a workaround, not a fix.

The canonicalisation rules (``NFR-UNIQ-002``) are:

* Keys sorted lexicographically.
* Floats serialised via ``repr()`` so ``1.0`` and ``1.00`` hash the
  same but ``1.0 + 1e-16`` hashes differently (preserving meaningful
  precision).
* ``NaN`` keys / values are rejected — propagating them through a
  hash is undefined because ``NaN != NaN``.
* Nested dicts / lists / tuples are recursed into; tuples are
  treated as ordered sequences.
* ``Decimal`` values are serialised via ``str()`` so their exact
  textual form round-trips through the hash.

This module does **not** depend on any heavy third-party library —
it participates in the unit-test suite without the P2 extras.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from brutex.strategies.errors import StrategyError


def canonicalise_params(params: Mapping[str, Any]) -> str:
    """Return a deterministic JSON-string encoding of *params*.

    This is the string that gets fed into the sha256 underlying
    :func:`combo_hash`. Exposed on its own so tests can assert on
    the canonical form directly without going through the hasher.

    Raises
    ------
    StrategyError
        If *params* contains a NaN float, a non-string dict key, or
        a value type the encoder can't represent
        (sets, bytes, custom objects, …).
    """
    return json.dumps(
        _canonicalise_value(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def combo_hash(
    *,
    strategy_id: str,
    params: Mapping[str, Any],
    data_snapshot_hash: str,
    code_commit_sha: str,
) -> str:
    """Return the 64-char sha256 hex digest of a combo's identity.

    Per ``FR-BF-003`` / ``NFR-UNIQ-001``, two combos collide iff all
    four inputs collide. The ``canonicalise_params`` step guarantees
    dict-key ordering does not affect the result.

    Parameters
    ----------
    strategy_id
        Unique identifier from :attr:`StrategyMeta.strategy_id`.
    params
        Concrete parameter dict for this combo. Canonicalised via
        :func:`canonicalise_params`.
    data_snapshot_hash
        sha256 hex of the data slice the combo will run against.
        Typically produced by
        :meth:`~brutex.data.catalog.Catalog.data_snapshot_hash`
        (``FR-CAT-004``).
    code_commit_sha
        Git commit SHA of the BruteX checkout running the combo.
        Included so a bug-fix release does not return a stale
        cached result.
    """
    if not strategy_id:
        raise StrategyError("combo_hash: strategy_id must be non-empty")
    if not data_snapshot_hash:
        raise StrategyError("combo_hash: data_snapshot_hash must be non-empty")
    if not code_commit_sha:
        raise StrategyError("combo_hash: code_commit_sha must be non-empty")

    canonical = canonicalise_params(params)
    payload = "\x1f".join(
        [
            strategy_id,
            canonical,
            data_snapshot_hash,
            code_commit_sha,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# Private helpers
# ────────────────────────────────────────────────────────────────────────


def _canonicalise_value(value: Any) -> Any:
    """Recursive one-step canonicalisation used by :func:`canonicalise_params`.

    Returns a value the stdlib ``json`` module can serialise directly.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            raise StrategyError("canonicalise_params: NaN floats are not hashable")
        # repr(float) round-trips exactly in CPython — two floats with
        # the same bit pattern produce the same repr string, so this
        # is the safest stable form.
        return repr(value)
    if isinstance(value, Decimal):
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
            out[k] = _canonicalise_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalise_value(v) for v in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonicalise_value(v) for v in value]
    raise StrategyError(
        f"canonicalise_params: unsupported value type {type(value).__name__}: {value!r}"
    )

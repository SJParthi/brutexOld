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
* ``NaN`` keys / values are rejected — propagating them through a
  hash is undefined because ``NaN != NaN``.
* Nested dicts / lists / tuples are recursed into; tuples are
  treated as ordered sequences.
* ``Decimal`` values are serialised via ``str()`` so their exact
  textual form round-trips through the hash.

This module imports ``fno_math`` (the BruteX Rust hot-kernel wheel,
shipped via DEC-P0-055 with bundled ``py.typed`` stubs) — no other
heavy third-party dependency.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import fno_math

from brutex.strategies.errors import StrategyError


def canonicalise_params(params: Mapping[str, Any]) -> str:
    """Return a deterministic JSON-string encoding of *params*.

    This is the string that gets fed into the BLAKE3 hasher underlying
    :func:`combo_hash`. Exposed on its own so tests can assert on the
    canonical form directly without going through the hasher.

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
        If any required string input is empty.
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

    canonical = canonicalise_params(params)
    payload = "\x1f".join(
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
    if isinstance(value, list | tuple):
        return [_canonicalise_value(v) for v in value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_canonicalise_value(v) for v in value]
    raise StrategyError(
        f"canonicalise_params: unsupported value type {type(value).__name__}: {value!r}"
    )

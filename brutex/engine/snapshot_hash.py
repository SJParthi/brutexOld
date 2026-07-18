"""Shared data-snapshot identity derivation — the leaf home of ``data_snapshot_hash``.

The SHARED snapshot-identity derivation (CLAUDE.md §11 COMMON, Golden Rule #4):
the ``brutex sweep-all`` launcher, :func:`brutex.engine.stage3_wire.run_stage3_pass`,
and the Stage-4 cascade snapshot stamp
(:func:`brutex.engine.cascade_wire.manifest_snapshot_hash`) all derive a run's
``data_snapshot_hash`` through this ONE helper.

It lives in its own leaf module — depending only on the ``brutex_identity``
kernel and :class:`~brutex.data.models.CandleBar` — so that both
:mod:`brutex.engine.stage3_wire` (which imports :mod:`brutex.engine.cascade_wire`
at module load) and :mod:`brutex.engine.cascade_wire` can import the derivation
WITHOUT forming an ``stage3_wire`` ⇄ ``cascade_wire`` import cycle
(DEC-SWEEP-UNKILLABLE-CORE-001; the #1533 py/cyclic-import extract-to-leaf
precedent). ``stage3_wire`` re-exports :func:`data_snapshot_hash` so the
public ``stage3_wire.data_snapshot_hash`` name (CLAUDE.md §2 Rule 4) is
unchanged.

DEC-RUST-RUNID-STORES-001 (the L14 conversion lane): the streamed digest —
the ``bxsnap2`` framing, the ISO-8601 timestamp render and the BLAKE3 fold —
now runs in the ``brutex_identity`` kernel
(:class:`brutex_identity.SnapshotHasher`); this wrapper owns the slice-sort
iteration order and the ``str(Decimal)`` price render (the exact
scale-preserving CPython text law, a deliberately-retained CPython primitive).
Byte-parity with the retired all-Python body is proven by the frozen
pre-conversion oracle fixture + the live differential suite, so every
recorded ``data_snapshot_hash`` is unchanged (Golden Rule #4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import brutex_identity

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping, Sequence

    from brutex.data.models import CandleBar

__all__ = ["data_snapshot_hash"]


def data_snapshot_hash(
    *,
    exchange: str,
    segment: str,
    start: str,
    end: str,
    slices: Mapping[tuple[str, str], Sequence[CandleBar]],
) -> str:
    """Return a real BLAKE3 content fingerprint of a loaded data window.

    The SHARED snapshot-identity derivation (CLAUDE.md §11 COMMON): the
    ``brutex sweep-all`` launcher hashes its full-window slices through this
    helper, and :func:`~brutex.engine.stage3_wire.run_stage3_pass` hashes the
    lockbox-excluded TRAIN slices through the SAME derivation (ADR-0007
    Amendment A1) — so a refined row's ``data_snapshot_hash`` honestly encodes
    the data it was evaluated on.

    FULL-CONTENT since DEC-SNAPSHOT-HASH-FULL-001 (preimage version
    ``bxsnap2``): every bar folds ALL SEVEN :class:`~brutex.data.models.CandleBar`
    fields (timestamp / open / high / low / close / volume / open_interest)
    into a STREAMED BLAKE3 (O(1) extra memory), so **any single-bar difference
    in any slice produces a different hash**. The v1 derivation (bar count +
    first/last timestamp+close per slice — a SAMPLED fingerprint) let an
    interior-bar re-pull edit alias the identity, so Bloom/LMDB dedup + the
    tuple cache served STALE results as fresh (Golden Rules #4/#7); it is
    retained ONLY as the private recount diagnostic
    :func:`brutex.engine.stage3_wire._data_snapshot_hash_v1`, never exported to
    any identity path. The ``bxsnap2|`` domain tag makes a v1/v2 preimage
    collision structurally impossible (including the degenerate all-empty-slices
    grid) and keeps the format provenance auditable.

    Honest complexity envelope (Golden Rule #10): NOT O(1) — O(total loaded
    bars), paid ONCE per launch / Stage-3 pass / recount over bars already
    resident in RAM; strictly smaller than the pre-existing unconditional
    per-launch ``catalog_reconcile`` full-byte rescan. Zero cost in any
    per-combo hot path (Rule 5 untouched). ``Decimal.__str__`` renders prices
    exactly (2dp-snapped at the lake-write boundary, DEC-SPOT-PRICE-2DP-001);
    a ``None`` open_interest renders ``"None"`` deterministically (the kernel
    folds the identical literal).

    Parameters
    ----------
    exchange, segment, start, end
        The data scope labels (the lake partition + the requested ISO window).
    slices
        Every loaded ``(symbol, interval) -> bars`` slice (empty slices
        participate as explicit zero-count entries).

    Returns
    -------
    str
        The 64-char lowercase BLAKE3 hex digest.
    """
    hasher = brutex_identity.SnapshotHasher(exchange, segment, start, end)
    for (symbol, interval), bars in sorted(slices.items()):
        hasher.add_slice(symbol, interval, len(bars))
        for bar in bars:
            hasher.add_bar(
                bar.timestamp,
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                bar.volume,
                bar.open_interest,
            )
    return str(hasher.hexdigest())

"""COMMON lake-slice read — reconcile → heal → feed → dedup (DEC-VIX-STAMP-WIRE-001).

One ``(exchange, segment, symbol, interval)`` slice of the Parquet lake, read the
way every engine consumer must read it:

1. **Reconcile** the DuckDB catalog from the on-disk Parquet first
   (:func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`) so a
   CLI-pulled / out-of-band lake is readable with no manual catalog step
   (DEC-CASCADE-RECONCILE-WIRE-001).
2. **Heal** what the reconcile's free L1 invariant piggyback detected
   (DEC-LAKE-HEAL-001): any poisoned-partition findings are healed BEFORE the
   feed consumes the slice (auto mode; ``BRUTEX_LAKE_HEAL=observe`` logs only).
   A :class:`~brutex.data.errors.LakeWriteHalt` raised by the healer propagates
   to the caller's halt handling — never swallowed.
3. **Feed** the window through the SAME :class:`~brutex.feed.replay.ReplayFeed`
   path the sweep uses (chronological, window-bounded).
4. **Dedup** keep-FIRST at the consumer's own input mouth
   (DEC-LAKE-READ-UNIQUE-001) — belt-and-braces: the feed's
   ``Catalog.get_bars`` already collapses stored duplicates keep-FIRST; this
   idempotent second pass pins the invariant per consumer (a clean stream
   passes through byte-identically), labelled by *dedup_source* so each
   consumer's drop events stay distinguishable in the logs.

Extracted VERBATIM from the per-coordinate body of
:func:`brutex.engine.cascade_wire.load_bars_for_manifest` (which now delegates
here) so the India-VIX reference-stamp read
(:mod:`brutex.engine.reference_wire`) shares ONE slice-read law with the
cascade replay instead of a copy (CLAUDE.md §11 COMMON).

This is offline batch work — O(partition files) reconcile + O(bars) read per
slice; no O(1)-total claim is made (Golden Rule #10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from datetime import datetime
    from pathlib import Path

    from brutex.data.catalog import Catalog
    from brutex.data.models import CandleBar

__all__ = ["load_lake_slice"]


def load_lake_slice(
    *,
    lake_root: Path,
    catalog: Catalog,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    dedup_source: str,
) -> list[CandleBar]:
    """Read one lake slice through the reconcile → heal → feed → dedup gauntlet.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    catalog
        An OPEN :class:`~brutex.data.catalog.Catalog`. The caller owns its
        lifecycle (open once, read many slices, close) — this function never
        opens or closes it.
    exchange, segment, symbol, interval
        The slice's partition coordinates
        (``bars/<exchange>/<segment>/<symbol>/<interval>/…``).
    start, end
        The tz-aware replay window (inclusive, the
        :class:`~brutex.feed.replay.ReplayFeed` contract). The read is BOUNDED
        by this window — never a whole-lake scan.
    dedup_source
        The label stamped on any ``lake.read.duplicates_dropped`` event from
        the belt-and-braces keep-FIRST pass, so each consumer's drops stay
        attributable (e.g. ``"cascade_wire:NIFTY/1minute"``).

    Returns
    -------
    list[CandleBar]
        The slice's bars, chronological, keep-FIRST unique per timestamp.
        Empty when the lake holds no bars for the coordinates/window — never
        an error, never a fabricated bar (Golden Rule #1).

    Raises
    ------
    brutex.data.errors.LakeWriteHalt
        Propagated from the L1 healer on a disk-full / OOM during a heal
        rewrite (the pull-halt contract; DEC-LAKE-HEAL-001).
    """
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
    from brutex.data.lake_heal import heal_after_reconcile, lake_heal_audit
    from brutex.data.lake_read import dedup_bars
    from brutex.feed.replay import ReplayFeed
    from brutex.notifications.sweep_alerts import emit_lake_heal_alert

    result = reconcile_catalog_from_lake(
        lake_root=lake_root,
        catalog=catalog,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
    )
    if result.findings:
        # DEC-LAKE-HEAL-001 L1: heal what the reconcile detected for free, so
        # the consumer reads clean bars. The audit sink is opened lazily
        # (findings-gated — a clean lake writes nothing).
        with lake_heal_audit(lake_root) as audit:
            heal_after_reconcile(
                result,
                lake_root=lake_root,
                catalog=catalog,
                audit=audit,
                on_report=lambda r: emit_lake_heal_alert(r, scope="reconcile"),
            )
    feed = ReplayFeed(
        catalog=catalog,
        lake_root=lake_root,
        exchange=exchange,
        segment=segment,
        symbols=[symbol],
        interval=interval,
        start=start,
        end=end,
    )
    # Belt-and-braces read gate (DEC-LAKE-READ-UNIQUE-001): the feed's
    # Catalog.get_bars already collapses stored duplicates keep-FIRST; this
    # idempotent second pass pins the invariant at the consumer's own input
    # mouth (a clean stream passes through byte-identically).
    clean_bars, _dropped = dedup_bars([event.bar for event in feed], source=dedup_source)
    return clean_bars

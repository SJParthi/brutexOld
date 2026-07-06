"""ONE alias-aware sweep-slice loader — spot verbatim, futures roll-stitched.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001: every sweep-family bar mouth (``sweep-all``
/ ``sweep`` / ``sweep-verify --recount`` / ``verify-lookahead`` / the cascade's
``load_bars_for_manifest`` / the options-sim replay) previously inlined the
same reconcile + :class:`~brutex.feed.replay.ReplayFeed` block. This module
extracts that block into ONE function, :func:`load_sweep_slice` (CLAUDE.md §11
COMMON), and makes it ALIAS-AWARE:

* a **spot / verbatim-dir symbol** takes EXACTLY the moved block — optional
  catalog reconcile, then one ``ReplayFeed`` read — byte-identical to the
  inline copies it replaces (fixture-parity-pinned);
* the **NIFTY-futures series alias**
  (:data:`brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL`) is resolved via
  :func:`brutex.data.futures_series.resolve_futures_series` into per-contract
  segments and stitched with an **exact IST-date ownership cut** —
  ``bar.timestamp.astimezone(IST).date()``, the SAME conversion
  ``stage1a.push`` keys on, so the loader's cut and the engine's roll cursor
  can never disagree about which contract owns a bar (both derive from the
  ONE ``roll_schedule`` of the same resolution).

The IST-DATE cut (never a ``09:15`` clamp) means muhurat-evening bars and
special-weekend sessions are owned by their own date, and the derived FNO
``1day`` bar (midnight-IST-stamped, living in the UTC month D−1 file) cuts
identically — the Catalog's ``first_ts``/``last_ts`` overlap predicate absorbs
the D−1 drift unchanged.

Duplicates across boundaries are STRUCTURALLY IMPOSSIBLE (disjoint IST-date
ownership) and a boundary monotonicity raise guards the impossible; WITHIN a
contract the catalog's keep-first dedup stands (DEC-LAKE-READ-UNIQUE-001).

Honest envelope (Golden Rule #10): loading is O(files overlapping the window)
per contract — the same envelope as ``Catalog.get_bars`` — and per-bar access
on the returned list stays O(1); no O(1)-total claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from brutex.calendars.nse import IST
from brutex.data.futures_series import (
    FuturesSeriesError,
    FuturesSeriesResolution,
    is_futures_series_symbol,
    resolve_futures_series,
)
from brutex.feed.replay import ReplayFeed

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from pathlib import Path

    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import ReconcileResult
    from brutex.data.models import CandleBar

__all__ = [
    "load_sweep_slice",
]


def _reconcile(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    on_reconcile: Callable[[ReconcileResult], None] | None,
) -> None:
    """Reconcile ONE ``(symbol, interval)`` coordinate from the lake into *cat*.

    The same lazy-import + call the inline mouths used
    (DEC-CATALOG-RECONCILE-WIRE-001); *on_reconcile* is the optional caller
    hook (the cascade's L1 heal rides it — DEC-LAKE-HEAL-001).
    """
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    result = reconcile_catalog_from_lake(
        lake_root=lake_root,
        catalog=cat,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
    )
    if on_reconcile is not None:
        on_reconcile(result)


def _read_symbol(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[CandleBar]:
    """One ``ReplayFeed`` read for one verbatim lake dir — the moved inline block."""
    feed = ReplayFeed(
        catalog=cat,
        lake_root=lake_root,
        exchange=exchange,
        segment=segment,
        symbols=[symbol],
        interval=interval,
        start=start,
        end=end,
    )
    return [event.bar for event in feed]


def load_sweep_slice(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    reconcile: bool = True,
    resolution: FuturesSeriesResolution | None = None,
    on_reconcile: Callable[[ReconcileResult], None] | None = None,
) -> list[CandleBar]:
    """Load ONE sweep ``(symbol, interval)`` slice — spot verbatim, alias stitched.

    Parameters
    ----------
    cat
        An open :class:`~brutex.data.catalog.Catalog` (caller-owned).
    lake_root
        The lake root directory.
    exchange, segment
        The launch coordinates (the alias requires ``NSE``/``FNO`` — enforced
        upstream by the scope gate; this loader reads the coordinates
        verbatim).
    symbol
        A verbatim lake dir symbol, or the NIFTY-futures series alias
        (:func:`~brutex.data.futures_series.is_futures_series_symbol`).
    interval
        The lake interval label.
    start, end
        Inclusive tz-aware replay window (the :class:`ReplayFeed` contract).
    reconcile
        When True (default), reconcile the catalog from the lake for each
        physical dir before reading (``cmd_sweep`` passes False — its
        no-reconcile law is preserved).
    resolution
        A pre-computed series resolution (``sweep-all`` resolves ONCE up
        front and passes it down per interval); ``None`` resolves here.
    on_reconcile
        Optional hook fired with each :class:`ReconcileResult` (the cascade's
        L1 heal seam). Only fired when *reconcile* is True.

    Returns
    -------
    list[CandleBar]
        The chronologically ordered slice (possibly empty for a spot symbol
        with no bars — the caller's preflight owns that refusal).

    Raises
    ------
    FuturesSeriesError
        Futures alias only: resolution refusals (interior hole etc.), a
        per-``(contract, interval)`` ZERO-BAR owned window (dir exists but the
        requested interval is empty — distinct message from a missing
        contract), or a boundary monotonicity break (never silent).
    """
    if not is_futures_series_symbol(symbol):
        # Spot / verbatim-dir path: EXACTLY the inline optional-reconcile +
        # ReplayFeed block the sweep mouths carried — moved, not rewritten.
        if reconcile:
            _reconcile(
                cat=cat,
                lake_root=lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                on_reconcile=on_reconcile,
            )
        return _read_symbol(
            cat=cat,
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
        )

    res = (
        resolution
        if resolution is not None
        else resolve_futures_series(
            lake_root,
            start=start.astimezone(IST).date(),
            end=end.astimezone(IST).date(),
        )
    )
    bars: list[CandleBar] = []
    for seg in res.segments:
        if reconcile:
            _reconcile(
                cat=cat,
                lake_root=lake_root,
                exchange=exchange,
                segment=segment,
                symbol=seg.contract_symbol,
                interval=interval,
                on_reconcile=on_reconcile,
            )
        # Feed-bound to the segment window (UTC of IST 00:00 own_start →
        # IST 23:59:59.999999 own_end), intersected with the operator window.
        # Bounds are normalised to UTC — the lake stores UTC timestamps and
        # the catalog's window filter compares dtype-strictly.
        seg_start = max(start, datetime.combine(seg.own_start, time.min, tzinfo=IST)).astimezone(
            UTC
        )
        seg_end = min(end, datetime.combine(seg.own_end, time.max, tzinfo=IST)).astimezone(UTC)
        if seg_end < seg_start:  # a degenerate intersection cannot own bars
            seg_bars: list[CandleBar] = []
        else:
            # …then the EXACT authoritative IST-date ownership cut — the SAME
            # ``astimezone(IST).date()`` conversion ``stage1a.push`` keys on.
            seg_bars = [
                bar
                for bar in _read_symbol(
                    cat=cat,
                    lake_root=lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=seg.contract_symbol,
                    interval=interval,
                    start=seg_start,
                    end=seg_end,
                )
                if seg.own_start <= bar.timestamp.astimezone(IST).date() <= seg.own_end
            ]
        if not seg_bars:
            hint = (
                ""
                if reconcile
                else " (this mouth reads without a catalog reconcile — run "
                "'brutex sweep-all' or reconcile the catalog first if the "
                "partition exists on disk)"
            )
            raise FuturesSeriesError(
                f"stored partition {seg.contract_symbol!r} / {interval!r} is EMPTY "
                f"over its owned window [{seg.own_start.isoformat()} → "
                f"{seg.own_end.isoformat()}] — re-pull the contract / run the "
                f"pipeline prebuild (dashboard ▶ Pull · full pipeline){hint}"
            )
        if bars and seg_bars[0].timestamp <= bars[-1].timestamp:
            raise FuturesSeriesError(
                f"boundary monotonicity broken: first bar of {seg.contract_symbol!r} "
                f"({seg_bars[0].timestamp.isoformat()}) is not after the previous "
                f"segment's last bar ({bars[-1].timestamp.isoformat()}) — the lake "
                "violates disjoint IST-date ownership (a genuine bug, never silent)"
            )
        bars.extend(seg_bars)
    return bars

"""Reference-series trade stamps — the generic per-trade context provider.

Track 1 stamps each completed trade with the **level of a reference series**
(currently India VIX) at the trade's entry and exit instants. India VIX is
**REFERENCE ONLY** (CLAUDE.md §1/§3): it is fetched + stored and carried as
``vix_at_entry`` / ``vix_at_exit`` on every :class:`~brutex.metrics.types.ClosedTrade`,
but it is **NEVER** part of the brute-force pattern combinations or the
60-condition vocabulary (DEC-VIX-TRADE-STAMP-001, ``docs/MIGRATION.md``). The
stamp is a downstream *filter* surface — "did this strategy only win when VIX
was low?" — not a signal input.

This module is the **generic** stamping mechanism, designed COMMON · DYNAMIC ·
SCALABLE (CLAUDE.md §11) so VIX is the *first* of N reference stamps, not a
hard-wired one-off:

* :class:`ReferenceSeries` — an O(1) ``timestamp → close`` lookup over one
  series' bars (VIX today; gap / ATR / breadth tomorrow add another instance,
  no re-architecting). Built once from a bar feed; every lookup is a single
  dict probe.
* :class:`TradeReferenceStamper` — the provider threaded into the trade
  simulator. It resolves the entry + exit timestamps against the VIX series and
  returns the two :class:`~decimal.Decimal` levels, or ``None`` when no VIX bar
  exists at that exact instant.

## No hallucination (CLAUDE.md Golden Rule #1)

A missing VIX bar at a trade's entry / exit timestamp yields ``None`` — never a
fabricated, interpolated, or carried-forward number. Each miss emits one
``reference_series.vix_miss`` structlog warning so the gap is visible, never
silent. The VIX series shares the spot index's trading calendar + timeframes,
so a well-formed aligned feed hits on every bar; the ``None`` path guards a
crafted / partial feed.

## Full precision (CLAUDE.md §4 — money-only rounding)

VIX is a volatility **index level**, not money: it keeps full
:class:`~decimal.Decimal` precision and is **never** routed through the
``brutex.money`` paisa rounding (that is for costs / display only — rounding a
metric breaks ranking).

## O(1) per trade (Golden Rule #5)

:class:`ReferenceSeries.level_at` is one dict lookup; stamping a trade is two
lookups. There is no scan over the series per trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

_LOG = structlog.get_logger(__name__)

#: The canonical name of the India VIX reference series (CLAUDE.md §3). Used as
#: the structlog ``series`` field so a future second series (gap / ATR) is
#: distinguishable in the logs without changing the call site.
VIX_SERIES_NAME: Final[str] = "india_vix"


@dataclass(frozen=True, slots=True)
class ReferenceSeries:
    """An O(1) ``timestamp → close`` lookup over one reference series' bars.

    The DYNAMIC building block (CLAUDE.md §11): VIX is one instance today; a
    later gap / ATR / breadth series is another instance built the same way, so
    the stamping path never needs re-architecting for a new reference.

    Parameters
    ----------
    name
        The series' canonical name (e.g. :data:`VIX_SERIES_NAME`), woven into
        the miss-warning log so multiple series are distinguishable.
    close_by_ts
        The ``{bar.timestamp → bar.close}`` map. Built once via
        :meth:`from_bars`; kept as a plain ``dict`` so :meth:`level_at` is a
        single O(1) probe.
    """

    name: str
    close_by_ts: dict[datetime, Decimal]

    @classmethod
    def from_bars(cls, name: str, bars: Sequence[CandleBar]) -> ReferenceSeries:
        """Build a :class:`ReferenceSeries` from a chronological bar feed.

        The series shares the index's calendar + timeframe, so it is aligned by
        bar ``timestamp``. A duplicate timestamp keeps the **last** bar (the
        normal lake feed has unique timestamps; this is a defensive tie-break,
        not an expected path).

        Parameters
        ----------
        name
            The series' canonical name (e.g. :data:`VIX_SERIES_NAME`).
        bars
            The reference series' bars (VIX 1-minute / resampled-TF bars for the
            same window + timeframe as the index being swept).

        Returns
        -------
        ReferenceSeries
            The O(1)-lookup series.
        """
        return cls(name=name, close_by_ts={bar.timestamp: bar.close for bar in bars})

    def level_at(self, ts: datetime) -> Decimal | None:
        """Return the series' close at ``ts``, or ``None`` if no bar exists there.

        One O(1) dict probe. ``None`` (never a fabricated value) when the exact
        instant has no bar — the no-hallucination contract (Golden Rule #1).
        """
        return self.close_by_ts.get(ts)


@dataclass(frozen=True, slots=True)
class TradeReferenceStamper:
    """Resolve a trade's entry/exit VIX levels — the provider for the simulator.

    Threaded into :class:`~brutex.engine.trade_sim.TradeSimWalker`; the walker
    calls :meth:`stamp` once per closed trade to obtain ``(vix_at_entry,
    vix_at_exit)``. Generic by construction — it owns a named
    :class:`ReferenceSeries`, so adding a second stamped series later is a new
    field + a wider return tuple, not a rewrite of the trade walk.

    Parameters
    ----------
    vix
        The India VIX :class:`ReferenceSeries`.
    """

    vix: ReferenceSeries

    @classmethod
    def from_vix_bars(cls, bars: Sequence[CandleBar]) -> TradeReferenceStamper:
        """Build a stamper from the India VIX bar feed (the common construction)."""
        return cls(vix=ReferenceSeries.from_bars(VIX_SERIES_NAME, bars))

    def stamp(self, entry_ts: datetime, exit_ts: datetime) -> tuple[Decimal | None, Decimal | None]:
        """Return ``(vix_at_entry, vix_at_exit)`` for a trade, ``None`` on a miss.

        Each ``None`` is logged once as ``reference_series.vix_miss`` so a gap is
        visible (Golden Rule #1) — never invented. O(1): two dict lookups.

        Parameters
        ----------
        entry_ts, exit_ts
            The trade's entry and exit timestamps (UTC, bar-aligned).

        Returns
        -------
        tuple[Decimal | None, Decimal | None]
            The VIX close at entry and at exit; ``None`` for either instant that
            has no VIX bar.
        """
        vix_entry = self.vix.level_at(entry_ts)
        if vix_entry is None:
            _LOG.warning("reference_series.vix_miss", series=self.vix.name, at="entry", ts=entry_ts)
        vix_exit = self.vix.level_at(exit_ts)
        if vix_exit is None:
            _LOG.warning("reference_series.vix_miss", series=self.vix.name, at="exit", ts=exit_ts)
        return vix_entry, vix_exit


__all__ = [
    "VIX_SERIES_NAME",
    "ReferenceSeries",
    "TradeReferenceStamper",
]

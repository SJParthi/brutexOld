"""Reference-series trade stamps — the generic per-trade context provider.

Track 1 stamps each completed trade with the **level of a reference series**
(currently India VIX) at the trade's entry and exit instants. India VIX is
**REFERENCE ONLY** (CLAUDE.md §1/§3): it is fetched + stored and carried as
``vix_at_entry`` / ``vix_at_exit`` on every :class:`~brutex.metrics.types.ClosedTrade`,
but it is **NEVER** part of the brute-force pattern combinations or the
condition vocabulary (DEC-VIX-TRADE-STAMP-001, ``docs/MIGRATION.md``). The
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
fabricated, interpolated, or carried-forward number. Each miss is visible,
never silent, through a MEMOISED + CAPPED warning scheme
(DEC-VIX-STAMP-WIRE-001 Stage D — a sweep re-stamps the same instants across
thousands of masks, so an uncapped per-miss WARNING would flood production
logs while telling the operator nothing new):

* the first :data:`MISS_WARN_DETAIL_CAP` DISTINCT missing instants each emit
  ONE detailed ``reference_series.vix_miss`` warning (a re-miss of an
  already-warned instant is memoised — counted, never re-warned);
* the next NEW distinct miss emits ONE ``reference_series.vix_miss_capped``
  sentinel (``distinct_misses=MISS_WARN_DETAIL_CAP``) and every further miss
  is silent in the log;
* the TRUE tally of every miss occurrence stays observable via
  :attr:`TradeReferenceStamper.miss_count` — the cap bounds the LOG VOLUME
  (≤ cap + 1 warnings per stamper) and the memo's memory (≤ cap instants),
  never the honesty of the count.

The VIX series shares the spot index's trading calendar + timeframes, so a
well-formed aligned feed hits on every bar; the ``None`` path guards a
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

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.lake_read import bars_by_ts_first

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

_LOG = structlog.get_logger(__name__)

#: The canonical name of the India VIX reference series (CLAUDE.md §3). Used as
#: the structlog ``series`` field so a future second series (gap / ATR) is
#: distinguishable in the logs without changing the call site.
VIX_SERIES_NAME: Final[str] = "india_vix"

#: Cap on the DETAILED per-instant miss warnings one stamper may emit
#: (DEC-VIX-STAMP-WIRE-001 Stage D). The first ``MISS_WARN_DETAIL_CAP``
#: DISTINCT missing instants each get one ``reference_series.vix_miss``
#: warning; the next NEW distinct miss emits the single
#: ``reference_series.vix_miss_capped`` sentinel; everything after is silent
#: in the log (still counted in :attr:`TradeReferenceStamper.miss_count`), so
#: the total warning volume per stamper is bounded at ``cap + 1``.
MISS_WARN_DETAIL_CAP: Final[int] = 20

#: Cap on the timestamps sampled into the one ``non_finite_level`` WARNING.
_NON_FINITE_SAMPLE_CAP: Final[int] = 5


@dataclass(slots=True)
class _MissWarnMemo:
    """Mutable miss-warning bookkeeping for ONE stamper (bounded + picklable).

    Deliberately NOT frozen — the tallies mutate per miss — and excluded from
    the owning stamper's equality / repr (``compare=False, repr=False`` at the
    field). Memory is BOUNDED: ``warned_instants`` never grows past
    :data:`MISS_WARN_DETAIL_CAP` entries regardless of how many distinct
    instants miss, so an all-miss multi-year sweep cannot balloon the memo.
    Plain stdlib state (``set`` / ``int`` / ``bool``), so the stamper stays
    picklable across the spawn-pool worker boundary
    (:class:`~brutex.engine.sweep_parallel._WorkerContext`); each worker
    process carries its OWN memo copy — the cap is per process, honestly.
    """

    warned_instants: set[datetime] = field(default_factory=set)
    miss_count: int = 0
    cap_warned: bool = False


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
        bar ``timestamp``. A duplicate timestamp keeps the **first** bar and is
        WARNED, never silent — the COMMON keep-FIRST read gate
        (:func:`brutex.data.lake_read.bars_by_ts_first`,
        DEC-LAKE-READ-UNIQUE-001; previously a silent last-wins dict, so a
        poisoned-era duplicate re-statement silently replaced the real close).

        A NON-FINITE close (``Infinity`` / ``NaN`` — only possible from
        out-of-band storage corruption: the ingest boundary and the engine
        read boundary both reject non-positive prices, but ``inf`` slips the
        latter's ``price <= 0`` gate and ``CandleBar`` accepts it) is SKIPPED
        + counted with ONE ``reference_series.non_finite_level`` WARNING per
        build — the instant then honestly stamps ``None`` (a miss), never a
        fabricated level and never an ``inf`` that would later abort the grid
        as a plain finite-validation :class:`ResultsStoreError` misdirected at
        upstream non-determinism (DEC-VIX-STAMP-WIRE-001 round-3 panel).

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
        first_by_ts = bars_by_ts_first(bars, source=f"reference_series:{name}")
        close_by_ts: dict[datetime, Decimal] = {}
        non_finite: list[datetime] = []
        for ts, bar in first_by_ts.items():
            # TWO screens (round-4 panel): Decimal.is_finite() kills
            # NaN/Infinity closes, but it is magnitude-blind — a FINITE
            # Decimal like 1E+400 saturates float() to inf, which would
            # sail through here and abort the whole grid downstream as
            # aggregate_trade_vix's float(stamp) hits the plain
            # finite-validation ResultsStoreError (the misdirection class).
            # Production-unreachable via the Float64 lake (max ~1.797e308
            # round-trips finite); this guards library embedders feeding
            # hand-built CandleBars. The instant stamps an honest None.
            if bar.close.is_finite() and math.isfinite(float(bar.close)):
                close_by_ts[ts] = bar.close
            else:
                non_finite.append(ts)
        if non_finite:
            _LOG.warning(
                "reference_series.non_finite_level",
                series=name,
                skipped=len(non_finite),
                sample=[ts.isoformat() for ts in non_finite[:_NON_FINITE_SAMPLE_CAP]],
                detail=(
                    "non-finite close in the reference partition (out-of-band storage "
                    "corruption) — the instant will stamp None; repair/quarantine the "
                    "corrupt partition file (the lake healer is row-removing only and "
                    "will not value-edit it)"
                ),
            )
        return cls(name=name, close_by_ts=close_by_ts)

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
    #: Per-stamper mutable miss bookkeeping (see :class:`_MissWarnMemo`) —
    #: excluded from equality/repr so two stampers over the same series stay
    #: equal regardless of how many misses each has observed.
    _miss_memo: _MissWarnMemo = field(default_factory=_MissWarnMemo, repr=False, compare=False)

    @classmethod
    def from_vix_bars(cls, bars: Sequence[CandleBar]) -> TradeReferenceStamper:
        """Build a stamper from the India VIX bar feed (the common construction)."""
        return cls(vix=ReferenceSeries.from_bars(VIX_SERIES_NAME, bars))

    @property
    def miss_count(self) -> int:
        """The TRUE total number of miss OCCURRENCES this stamper observed.

        Every ``None`` stamp counts — entry and exit each, repeats of the same
        instant included — regardless of the warning memo/cap state, so the
        log-volume cap never hides the honest tally (Golden Rule #1 / #10).
        """
        return self._miss_memo.miss_count

    def stamp(self, entry_ts: datetime, exit_ts: datetime) -> tuple[Decimal | None, Decimal | None]:
        """Return ``(vix_at_entry, vix_at_exit)`` for a trade, ``None`` on a miss.

        A ``None`` is never invented into a value (Golden Rule #1) and never
        silent: each miss is tallied in :attr:`miss_count` and surfaced through
        the memoised + capped ``reference_series.vix_miss`` /
        ``vix_miss_capped`` warning scheme (module docstring;
        :data:`MISS_WARN_DETAIL_CAP`). O(1) per trade: two dict lookups, plus
        O(1) memo bookkeeping on the miss path only.

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
            self._record_miss("entry", entry_ts)
        vix_exit = self.vix.level_at(exit_ts)
        if vix_exit is None:
            self._record_miss("exit", exit_ts)
        return vix_entry, vix_exit

    def _record_miss(self, at: str, ts: datetime) -> None:
        """Tally one miss + emit the memoised/capped warning (module docstring).

        O(1): one set probe + at most one set insert. The memo set is bounded
        at :data:`MISS_WARN_DETAIL_CAP` entries — after the cap, a NEW distinct
        instant is deliberately NOT memoised (the sentinel already said the
        details stopped; unbounded memoisation would trade a log flood for a
        memory flood).
        """
        memo = self._miss_memo
        memo.miss_count += 1
        if ts in memo.warned_instants:
            return  # already detailed once — memoised, never re-warned
        if len(memo.warned_instants) < MISS_WARN_DETAIL_CAP:
            memo.warned_instants.add(ts)
            _LOG.warning("reference_series.vix_miss", series=self.vix.name, at=at, ts=ts)
            return
        if not memo.cap_warned:
            memo.cap_warned = True
            _LOG.warning(
                "reference_series.vix_miss_capped",
                series=self.vix.name,
                distinct_misses=MISS_WARN_DETAIL_CAP,
                detail="per-instant detail cap reached — further misses are "
                "counted silently (see TradeReferenceStamper.miss_count)",
            )


__all__ = [
    "MISS_WARN_DETAIL_CAP",
    "VIX_SERIES_NAME",
    "ReferenceSeries",
    "TradeReferenceStamper",
]

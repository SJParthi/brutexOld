"""Top gainers / losers screener — rank lake instruments by window % change.

A **reference-only** screener over the BruteX data lake (CLAUDE.md §3 — the
brute-force ENGINE is untouched; this only reads the lake). Given a window
``[start, end]`` and a timeframe ``interval``, it ranks every instrument the
injected :class:`LakeReader` exposes by its **% price change across the
window** and returns the top-N true gainers and top-N true losers.

## What "% change over the window" means

For one instrument's window bars ``b[0..k]`` the move is measured from the
**first bar's open** (the price at the window's opening boundary) to the
**last bar's close** (the price at the window's closing boundary)::

    pct_change = (last_close − first_open) / first_open × 100

This captures the *complete* move inside the window and is well-defined even
for a single-bar window (that bar's own open → close). The two endpoint
prices are snapped to the 2-decimal rupee grid via
:func:`brutex.money.round_money` (``DEC-SPOT-PRICE-2DP-001``) so the displayed
prices honour the money rule; the **% change itself is a statistical metric
and is NEVER money-rounded** (CLAUDE.md §4 / ``DEC-MONEY-2DP-001`` is
display-money-only) — it keeps full :class:`~decimal.Decimal` precision.

The ``(value − base) / base × 100`` arithmetic is **not re-implemented** here:
it delegates to the single source of truth
:func:`brutex.data.spot_features.pct_change` (``DEC-SPOT-PCT-CHANGE-001``,
``docs/MIGRATION.md`` entry #346), so the screener can never drift from the
rest of the codebase (CLAUDE.md §11 COMMON — the jscpd duplication gate).

## Gainers vs losers (sign-split)

A **gainer** must have *gained* (``pct_change > 0``); a **loser** must have
*lost* (``pct_change < 0``). A flat instrument (``pct_change == 0``) is in
neither list. Splitting by sign keeps the names honest (Golden Rule #1) and
makes the two lists disjoint — the same instrument can never be both a top
gainer and a top loser.

## Efficient lake read (no whole-lake scan)

The injected :class:`LakeReader.read_window` mirrors the lake's own
``Catalog.get_bars`` — it reads ONLY the partitions that overlap
``[start, end]`` for the one instrument, never the whole lake. Building a
:class:`Mover` from those bars is **O(1) in the number of window bars**
(:func:`_build_mover` touches only the first bar, the last bar, and the
count — it does NOT scan the window).

## Honest envelope (Golden Rule #10)

* **Not O(1) total.** Ranking ``M`` instruments is inherently
  ``O(M·log M)`` for the sort plus one ``O(window)`` read per instrument —
  the *per-instrument change* is O(1), the whole screen is not, and this
  module never claims otherwise.
* **Local data only.** The screen ranks whatever the operator has actually
  ingested into the lake; an instrument with no bars in the window is
  reported as *skipped*, never invented. No prices are fabricated
  (Golden Rule #1).

Decision ID: ``DEC-MOVERS-GAINERS-001`` (``docs/MIGRATION.md`` entry #699).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from brutex.data.models import CandleBar
from brutex.data.spot_features import pct_change
from brutex.money import round_money


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """Identifies one lake instrument to screen.

    The triple mirrors the lake's partition coordinates (CLAUDE.md §6 — the
    ``groww_symbol`` string + exchange + segment); the screener never needs
    the numeric vendor id. The :attr:`sort_key` gives every ranking a total,
    deterministic tiebreak.

    Attributes
    ----------
    symbol
        The canonical instrument symbol (e.g. ``"NIFTY"``).
    exchange
        The exchange token (e.g. ``"NSE"`` / ``"BSE"``).
    segment
        The lake segment token (e.g. ``"INDEX"`` / ``"CASH"`` / ``"FNO"``).
    """

    symbol: str
    exchange: str
    segment: str

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Return the total-order tiebreak key ``(symbol, exchange, segment)``."""
        return (self.symbol, self.exchange, self.segment)


@dataclass(frozen=True, slots=True)
class Mover:
    """One instrument's measured move across the screen window.

    Attributes
    ----------
    ref
        The instrument this move belongs to.
    base_price
        The window's opening price — the first bar's ``open``, snapped to the
        2-decimal rupee grid (:func:`brutex.money.round_money`).
    last_price
        The window's closing price — the last bar's ``close``, snapped to the
        2-decimal rupee grid.
    pct_change
        ``(last_price − base_price) / base_price × 100`` at full
        :class:`~decimal.Decimal` precision — a statistical metric, NEVER
        money-rounded (CLAUDE.md §4).
    bar_count
        Number of bars that fell inside the window for this instrument.
    """

    ref: InstrumentRef
    base_price: Decimal
    last_price: Decimal
    pct_change: Decimal
    bar_count: int


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """The outcome of one gainers/losers screen.

    Attributes
    ----------
    gainers
        Up to ``top_n`` true gainers (``pct_change > 0``), ordered by
        descending % change with a ``(symbol, exchange, segment)`` tiebreak.
    losers
        Up to ``top_n`` true losers (``pct_change < 0``), ordered by ascending
        % change (most negative first) with the same tiebreak.
    skipped
        Instruments with no usable window data (no bars, or a zero opening
        price), in ``(symbol, exchange, segment)`` order. Reported honestly,
        never ranked (Golden Rule #1).
    """

    gainers: tuple[Mover, ...]
    losers: tuple[Mover, ...]
    skipped: tuple[InstrumentRef, ...]


@runtime_checkable
class LakeReader(Protocol):
    """Read-side seam over the lake for the screener (the injected dependency).

    A real implementation wraps the lake's ``InstrumentStore`` (to enumerate
    what is ingested) + ``Catalog.get_bars`` (to read a window's bars for one
    instrument); tests inject a fake that returns canned :class:`CandleBar`
    lists. The screener talks ONLY through this protocol, so the ranking core
    stays pure + 100%-unit-testable without the heavy Parquet/DuckDB deps.
    """

    def list_instruments(self) -> Sequence[InstrumentRef]:
        """Return the instruments currently available in the lake to screen."""
        ...

    def read_window(
        self,
        ref: InstrumentRef,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[CandleBar]:
        """Return *ref*'s bars in ``[start, end]`` at *interval*, ascending by time.

        Reads only the partitions overlapping the window (never the whole
        lake). Returns an empty sequence when the instrument has no bars in
        the window — the screener then reports it as *skipped*.
        """
        ...


def _build_mover(ref: InstrumentRef, bars: Sequence[CandleBar]) -> Mover | None:
    """Reduce one instrument's window *bars* to a :class:`Mover`, or ``None``.

    O(1) in ``len(bars)`` — only the first bar's open, the last bar's close,
    and the count are read; the window is never scanned. Returns ``None`` (the
    caller reports the instrument as *skipped*) when there are no bars, or when
    the opening price snaps to zero so the % change is undefined (the
    :func:`brutex.data.spot_features.pct_change` zero-base guard).
    """
    if not bars:
        return None
    base_price = round_money(bars[0].open)
    last_price = round_money(bars[-1].close)
    change = pct_change(last_price, base_price)
    if change is None:  # base price is 0 — % change is undefined; skip honestly.
        return None
    return Mover(
        ref=ref,
        base_price=base_price,
        last_price=last_price,
        pct_change=change,
        bar_count=len(bars),
    )


def rank_movers(
    movers: Iterable[Mover],
    *,
    top_n: int,
) -> tuple[tuple[Mover, ...], tuple[Mover, ...]]:
    """Split *movers* into the top-N gainers and top-N losers (pure).

    The pure ranking core. Gainers are the ``pct_change > 0`` movers ordered by
    descending % change; losers are the ``pct_change < 0`` movers ordered by
    ascending % change (most negative first). Flat movers (``== 0``) appear in
    neither. Every order has a total, deterministic
    ``(symbol, exchange, segment)`` tiebreak so equal % changes never reorder
    run-to-run (Golden Rule #4).

    Parameters
    ----------
    movers
        The per-instrument :class:`Mover` records (any iterable).
    top_n
        How many gainers and losers to return (each capped independently).
        Must be ``>= 1``.

    Returns
    -------
    tuple[tuple[Mover, ...], tuple[Mover, ...]]
        ``(gainers, losers)`` — each at most ``top_n`` long.

    Raises
    ------
    ValueError
        If ``top_n < 1``.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    pool = list(movers)
    gainers = sorted(
        (m for m in pool if m.pct_change > 0),
        key=lambda m: (-m.pct_change, m.ref.symbol, m.ref.exchange, m.ref.segment),
    )
    losers = sorted(
        (m for m in pool if m.pct_change < 0),
        key=lambda m: (m.pct_change, m.ref.symbol, m.ref.exchange, m.ref.segment),
    )
    return tuple(gainers[:top_n]), tuple(losers[:top_n])


def screen_gainers_losers(
    reader: LakeReader,
    *,
    interval: str,
    start: datetime,
    end: datetime,
    top_n: int,
) -> ScreenResult:
    """Rank the lake's instruments by % change over ``[start, end]``.

    Enumerates the *reader*'s instruments, reads each one's window bars,
    reduces them to a :class:`Mover` (O(1) per instrument), and ranks the
    population via :func:`rank_movers`. Instruments with no usable window data
    are collected into :attr:`ScreenResult.skipped` (sorted), never invented.

    Parameters
    ----------
    reader
        The injected :class:`LakeReader`.
    interval
        The lake timeframe to read (e.g. ``"1day"``).
    start, end
        The inclusive UTC window bounds; ``start`` must be ``<= end``.
    top_n
        How many gainers and losers to return. Must be ``>= 1``.

    Returns
    -------
    ScreenResult
        The top-N gainers, top-N losers, and the skipped instruments.

    Raises
    ------
    ValueError
        If ``top_n < 1`` or ``start > end``.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    if start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")
    movers: list[Mover] = []
    skipped: list[InstrumentRef] = []
    for ref in reader.list_instruments():
        bars = reader.read_window(ref, interval=interval, start=start, end=end)
        mover = _build_mover(ref, bars)
        if mover is None:
            skipped.append(ref)
        else:
            movers.append(mover)
    gainers, losers = rank_movers(movers, top_n=top_n)
    return ScreenResult(
        gainers=gainers,
        losers=losers,
        skipped=tuple(sorted(skipped, key=lambda r: r.sort_key)),
    )


__all__ = [
    "InstrumentRef",
    "LakeReader",
    "Mover",
    "ScreenResult",
    "rank_movers",
    "screen_gainers_losers",
]

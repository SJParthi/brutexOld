"""1-minute-path math for the results-depth capture pass (DEC-SWEEP-RESULTS-DEPTH-001).

The sweep simulates trades at the tuple's OWN timeframe, so per-trade MFE/MAE
excursions are folded from sweep-TF bar highs/lows and the portfolio drawdown is
realized-equity-only (recon: no intraday bar-path drawdown exists anywhere).
R22 demands the 1-MINUTE truth: MAE/MFE from the real minute path — a gap-open
minute far beyond the stop shows its TRUE adverse depth — and intraday equity
drawdown measured on the minute path (close-only DD understates real pain).

This module is that math, as a **wheel-free stdlib leaf** (no ``polars`` /
``numpy`` / Rust wheels — the design's F.10 envelope): it consumes
already-loaded :class:`~brutex.data.models.CandleBar` sequences and returns
plain frozen records. Loading the 1-minute bars is the caller's job (the
run-end pass does ONE batched lake read per (instrument, window) — recon Q6's
batch-load-then-slice pattern; this module never touches I/O).

## Honesty envelope (Golden Rule #1 / #10)

* Missing 1-minute coverage is a first-class :class:`PathStatus` — ``NO_1M_DATA``
  (no minute bars in the trade's span / no index at all) or ``PARTIAL`` (some
  minutes present but an endpoint minute missing) — never a fabricated number.
  Interior single-minute holes inside a covered span are NOT detected (that
  would need calendar-complete minute enumeration); the endpoint check is the
  honest cheap proxy, documented here.
* The per-trade 1m excursion covers the minutes ``[entry_ts, exit_ts]``
  inclusive; the exit minute's contribution mirrors the walker's own exit-bar
  convention via ``exit_bar_full_range`` (full high/low for a level /
  end-of-data exit — the walker folds the full exit-bar range there too — and
  OPEN-only for a next-open / forced-open fill, where the position was already
  closed at that bar's open). At ``tf == 1minute`` the fold is therefore
  bit-identical to the walker's own MFE/MAE (property-pinned).
* The session equity low marks the open trade at each minute's WORST price
  (low for a long, high for a short). Equity PEAKS are sampled at session
  closes (we track adverse marks, not favourable ones), so the drawdown is a
  lower bound on true tick-path drawdown — stated, not hidden.
* Rupees→paisa uses the COMMON :func:`brutex.money.rupees_to_paisa`
  (``round_money`` half-up × 100 — the same law as the wheel-bound
  ``stage1a._price_to_paisa`` seam; equality is test-pinned).

## O() envelope

Building a :class:`MinuteIndex` is O(N_bars) once per (instrument, window);
:meth:`MinuteIndex.span_slice` is O(log N) bisect; a trade's excursion fold is
O(minutes-in-trade); a session equity series is one O(N_bars + N_trades) pass.
Deterministic and pure: no globals, no wall-clock, no RNG, no I/O.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from brutex.calendars.nse import IST
from brutex.engine.exit_index import bars_strictly_increasing
from brutex.models import BruteXError
from brutex.money import rupees_to_paisa
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from decimal import Decimal

    from brutex.data.models import CandleBar

__all__ = [
    "EquityTradeLike",
    "MinuteIndex",
    "MinutePathError",
    "PathStatus",
    "SessionEquityPoint",
    "TradePath1m",
    "ist_session_dates",
    "minute_path_max_drawdown",
    "session_equity_series",
    "trade_excursions_1m",
]


class MinutePathError(BruteXError):
    """Raised on a malformed minute-path input (unsorted bars, bad span, HOLD, …)."""


class PathStatus(Enum):
    """Honesty verdict for a 1-minute-path computation (Golden Rule #1).

    * ``OK`` — the 1m path fully covered the requested span (both endpoint
      minutes present for an excursion; every session had at least one minute
      bar for an equity series).
    * ``NO_1M_DATA`` — no 1-minute bars at all for the span (or no index was
      supplied); every path-derived value is ``None``, never fabricated.
    * ``PARTIAL`` — some minutes present but coverage incomplete (an endpoint
      minute missing / a session with zero minute bars). Values computed over
      the covered minutes are returned (an honest lower bound), flagged so a
      consumer never averages them with fully-covered ones.
    """

    OK = "ok"
    NO_1M_DATA = "no_1m_data"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class MinuteIndex:
    """A bisect-able view over one instrument's loaded 1-minute bars.

    Built ONCE per (instrument, window) from the batched lake read; every
    per-trade slice is then an O(log N) bisect + O(minutes-in-trade) fold —
    never a re-read, never a scan from bar 0.

    Parameters
    ----------
    bars
        The chronologically ordered (strictly increasing timestamps,
        de-duplicated by the caller's read gate) 1-minute bars.
    timestamps
        ``bars``' timestamps as a parallel tuple — the bisect key array.
        Derived by :meth:`from_bars`; never supply a diverging copy.
    """

    bars: tuple[CandleBar, ...]
    timestamps: tuple[datetime, ...]

    @classmethod
    def from_bars(cls, bars: Sequence[CandleBar]) -> MinuteIndex:
        """Build a :class:`MinuteIndex`, validating strict chronological order.

        Raises
        ------
        MinutePathError
            If the timestamps are not strictly increasing (duplicates /
            reordering — the caller must dedup via the COMMON lake read gate
            before indexing; a last-wins silent fix here would hide poison).
        """
        if not bars_strictly_increasing(bars):
            raise MinutePathError(
                "MinuteIndex.from_bars: bar timestamps must be strictly increasing "
                "(dedup + sort at the read boundary before indexing)"
            )
        frozen = tuple(bars)
        return cls(bars=frozen, timestamps=tuple(bar.timestamp for bar in frozen))

    def span_slice(self, start: datetime, end: datetime) -> tuple[int, int]:
        """Return ``(lo, hi)`` such that ``bars[lo:hi]`` are the bars in ``[start, end]``.

        Both endpoints INCLUSIVE; O(log N) bisect. An empty span returns
        ``lo == hi``.

        Raises
        ------
        MinutePathError
            If ``start > end``.
        """
        if start > end:
            raise MinutePathError(f"span_slice: start {start!r} is after end {end!r}")
        return bisect_left(self.timestamps, start), bisect_right(self.timestamps, end)


@dataclass(frozen=True, slots=True)
class TradePath1m:
    """One trade's 1-minute-path excursions (or the honest absence thereof).

    ``mae_paisa`` / ``mfe_paisa`` are non-negative magnitudes exactly like
    :class:`~brutex.metrics.types.ClosedTrade`'s TF-granularity twins; both are
    ``None`` iff ``status is PathStatus.NO_1M_DATA`` (never a fabricated zero).
    A ``PARTIAL`` status carries values folded over the covered minutes only —
    an honest lower bound, flagged.
    """

    mae_paisa: int | None
    mfe_paisa: int | None
    status: PathStatus


def trade_excursions_1m(
    index: MinuteIndex,
    *,
    entry_ts: datetime,
    exit_ts: datetime,
    entry_fill_paisa: int,
    direction: Side,
    exit_bar_full_range: bool = True,
) -> TradePath1m:
    """Fold one trade's REAL 1-minute path into MAE/MFE magnitudes.

    Every minute bar in ``[entry_ts, exit_ts]`` contributes its full high/low
    (a gap-open minute beyond the stop therefore shows its TRUE adverse depth,
    R22) — except the EXIT minute when ``exit_bar_full_range`` is ``False``,
    which contributes only its OPEN (the walker's next-open / forced-open fill
    convention: the position was already closed at that bar's open, so folding
    its later range would fabricate exposure). Map the walker's exit reasons as
    ``exit_bar_full_range = reason in {stop_loss, take_profit, end_of_data}``
    (the reasons whose exit bar the walker itself folds in full).

    Parameters
    ----------
    index
        The (instrument, window)'s :class:`MinuteIndex`.
    entry_ts, exit_ts
        The trade's entry / exit bar timestamps (UTC, from the sweep-TF sim).
    entry_fill_paisa
        The exact entry fill (int64 paisa) — excursions are measured from it.
    direction
        ``Side.BUY`` or ``Side.SELL`` (``HOLD`` rejected).
    exit_bar_full_range
        Whether the exit minute contributes its full high/low (see above).

    Returns
    -------
    TradePath1m
        The folded magnitudes + the honesty status: ``NO_1M_DATA`` when the
        span holds zero minute bars, ``PARTIAL`` when an endpoint minute is
        missing, else ``OK``.

    Raises
    ------
    MinutePathError
        If ``direction`` is ``HOLD`` or ``entry_ts > exit_ts``.
    """
    if direction is Side.HOLD:
        raise MinutePathError("trade_excursions_1m: direction must be BUY or SELL, not HOLD")
    lo, hi = index.span_slice(entry_ts, exit_ts)
    if lo == hi:
        return TradePath1m(mae_paisa=None, mfe_paisa=None, status=PathStatus.NO_1M_DATA)
    status = (
        PathStatus.OK
        if index.timestamps[lo] == entry_ts and index.timestamps[hi - 1] == exit_ts
        else PathStatus.PARTIAL
    )
    mfe = 0
    mae = 0
    for i in range(lo, hi):
        bar = index.bars[i]
        if index.timestamps[i] == exit_ts and not exit_bar_full_range:
            open_paisa = rupees_to_paisa(bar.open)
            high_paisa = open_paisa
            low_paisa = open_paisa
        else:
            high_paisa = rupees_to_paisa(bar.high)
            low_paisa = rupees_to_paisa(bar.low)
        if direction is Side.BUY:
            favourable = high_paisa - entry_fill_paisa
            adverse = entry_fill_paisa - low_paisa
        else:  # Side.SELL — profit when price falls.
            favourable = entry_fill_paisa - low_paisa
            adverse = high_paisa - entry_fill_paisa
        mfe = max(mfe, favourable)
        mae = max(mae, adverse)
    return TradePath1m(mae_paisa=mae, mfe_paisa=mfe, status=status)


class EquityTradeLike(Protocol):
    """Structural trade shape :func:`session_equity_series` consumes.

    Satisfied by ``brutex.engine.trade_capture.CapturedTrade`` (which cannot be
    imported here — ``trade_capture`` imports THIS module; the Protocol keeps
    the leaf cycle-free, the ``exit_index`` precedent).
    """

    @property
    def entry_ts(self) -> datetime:
        """The trade's entry instant (UTC, tz-aware)."""

    @property
    def exit_ts(self) -> datetime:
        """The trade's exit instant (UTC, tz-aware, ``>= entry_ts``)."""

    @property
    def direction(self) -> Side:
        """``Side.BUY`` or ``Side.SELL``."""

    @property
    def pnl_paisa(self) -> int:
        """The trade's realized P&L (signed int64 paisa)."""

    @property
    def entry_px(self) -> Decimal:
        """The exact entry fill price (2dp Decimal rupees)."""


@dataclass(frozen=True, slots=True)
class SessionEquityPoint:
    """One IST session's equity sample on the minute path.

    ``equity_close_paisa`` is the realized equity after every trade that
    exited on/before this session (close-only truth — always computable);
    ``equity_intraday_low_paisa`` is the minute-path low — realized equity
    plus the open trade's adverse mark at each minute's worst price — or
    ``None`` when the session has no 1-minute bars (honest gap, never 0).
    """

    session_date: date
    equity_close_paisa: int
    equity_intraday_low_paisa: int | None
    trades_closed: int


def _ist_date(ts: datetime) -> date:
    """Return the IST calendar date of a UTC instant (the ONE session key)."""
    return ts.astimezone(IST).date()


def ist_session_dates(bars: Sequence[CandleBar]) -> tuple[date, ...]:
    """Return the sorted unique IST calendar dates of *bars* — the session axis.

    The window's session list for :func:`session_equity_series`, derived from
    the SAME bars the sweep loaded (dates with data, never a fabricated
    calendar). One O(N_bars) pass.
    """
    return tuple(sorted({_ist_date(bar.timestamp) for bar in bars}))


def _day_slice(index: MinuteIndex, day: date) -> tuple[int, int]:
    """Return the ``(lo, hi)`` bar slice of IST calendar date *day* (half-open hi)."""
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    end = start + timedelta(days=1)
    return bisect_left(index.timestamps, start), bisect_left(index.timestamps, end)


def session_equity_series(
    index: MinuteIndex | None,
    trades: Sequence[EquityTradeLike],
    *,
    sessions: Sequence[date],
    start_equity_paisa: int,
) -> tuple[tuple[SessionEquityPoint, ...], PathStatus]:
    """Build the per-session minute-path equity series for one combo's trades.

    One chronological pass: per session, the intraday equity low is
    ``min`` over the session's minutes of (realized equity so far + the open
    trade's adverse mark at that minute's worst price — low for a long, high
    for a short); the close is the realized equity after the session's exits.
    This is the R22 intraday-path drawdown input the realized-only sweep curve
    cannot see (an open trade's interim heat never dents ``max_drawdown_*``).

    Parameters
    ----------
    index
        The instrument's :class:`MinuteIndex`, or ``None`` when no 1-minute
        data was available — the series degrades to close-only points
        (``equity_intraday_low_paisa=None``) with ``NO_1M_DATA`` status.
    trades
        The combo's captured trades, chronological and non-overlapping (the
        walker's single-position law); every exit's IST date must be one of
        ``sessions``. An empty sequence is first-class (a zero-trade combo):
        the series is flat at ``start_equity_paisa``.
    sessions
        The window's IST session dates, strictly increasing (derive from the
        sweep's own bars — the same window the trades came from).
    start_equity_paisa
        The notional capital base (``> 0``) — the sweep's own constant.

    Returns
    -------
    tuple[tuple[SessionEquityPoint, ...], PathStatus]
        One point per session, plus the coverage status: ``NO_1M_DATA`` (no
        index / empty index), ``PARTIAL`` (some session had zero minute bars),
        else ``OK``.

    Raises
    ------
    MinutePathError
        On a non-positive base, unsorted ``sessions``, a ``HOLD`` trade,
        out-of-order / overlapping trades, or a trade exiting outside
        ``sessions``.
    """
    if start_equity_paisa <= 0:
        raise MinutePathError(
            f"session_equity_series: start_equity_paisa must be > 0, got {start_equity_paisa}"
        )
    if any(sessions[i - 1] >= sessions[i] for i in range(1, len(sessions))):
        raise MinutePathError("session_equity_series: sessions must be strictly increasing")
    session_set = frozenset(sessions)
    previous_exit: datetime | None = None
    for trade in trades:
        if trade.direction is Side.HOLD:
            raise MinutePathError("session_equity_series: trade direction must be BUY or SELL")
        if previous_exit is not None and trade.entry_ts <= previous_exit:
            raise MinutePathError(
                "session_equity_series: trades must be chronological and non-overlapping "
                f"(entry {trade.entry_ts!r} is not after the previous exit {previous_exit!r})"
            )
        previous_exit = trade.exit_ts
        if _ist_date(trade.exit_ts) not in session_set:
            raise MinutePathError(
                f"session_equity_series: trade exit date {_ist_date(trade.exit_ts)} "
                "is not one of the supplied sessions"
            )

    live_index = index if index is not None and len(index.bars) > 0 else None
    partial = False
    points: list[SessionEquityPoint] = []
    realized = start_equity_paisa
    pointer = 0
    n_trades = len(trades)
    for day in sessions:
        closed_today = 0
        session_low: int | None = None
        if live_index is not None:
            lo, hi = _day_slice(live_index, day)
            if lo == hi:
                partial = True
            else:
                session_low = realized
                for i in range(lo, hi):
                    bar = live_index.bars[i]
                    ts = live_index.timestamps[i]
                    # Flush trades fully closed strictly BEFORE this minute.
                    while pointer < n_trades and trades[pointer].exit_ts < ts:
                        realized += trades[pointer].pnl_paisa
                        closed_today += 1
                        pointer += 1
                        session_low = min(session_low, realized)
                    candidate = realized
                    if pointer < n_trades:
                        trade = trades[pointer]
                        if trade.entry_ts <= ts <= trade.exit_ts:
                            entry_paisa = rupees_to_paisa(trade.entry_px)
                            if trade.direction is Side.BUY:
                                mark = rupees_to_paisa(bar.low) - entry_paisa
                            else:
                                mark = entry_paisa - rupees_to_paisa(bar.high)
                            candidate = realized + mark
                            if ts == trade.exit_ts:
                                realized += trade.pnl_paisa
                                closed_today += 1
                                pointer += 1
                    session_low = min(session_low, candidate, realized)
        # End-of-session flush: exits dated today whose minute bar was absent
        # (partial 1m data) or when no index exists at all — the CLOSE stays
        # exact regardless of minute coverage (close-only truth).
        while pointer < n_trades and _ist_date(trades[pointer].exit_ts) <= day:
            realized += trades[pointer].pnl_paisa
            closed_today += 1
            pointer += 1
        points.append(
            SessionEquityPoint(
                session_date=day,
                equity_close_paisa=realized,
                equity_intraday_low_paisa=session_low,
                trades_closed=closed_today,
            )
        )
    if live_index is None:
        status = PathStatus.NO_1M_DATA
    elif partial:
        status = PathStatus.PARTIAL
    else:
        status = PathStatus.OK
    return tuple(points), status


def minute_path_max_drawdown(
    points: Sequence[SessionEquityPoint],
    *,
    start_equity_paisa: int | None = None,
) -> tuple[int, float]:
    """Max drawdown over a session equity series, folding the intraday lows.

    The peak tracks the highest equity seen at a session CLOSE (plus the
    starting base when supplied — recommended: the first session's own dip
    below the base is real drawdown); each session's trough is
    ``min(intraday_low, close)`` (a ``None`` low degrades to the close —
    close-only for that session, never fabricated). Returns
    ``(max_dd_paisa, max_dd_pct)`` where the TWO maxima are tracked
    INDEPENDENTLY: ``max_dd_paisa`` is the largest absolute drop and
    ``max_dd_pct`` is the largest ``drop / peak`` ratio over ALL points —
    a smaller absolute drop measured from a smaller earlier peak can be the
    larger percentage, so the two maxima may occur at DIFFERENT points
    (the pre-amendment behaviour reported the pct AT the absolute-max point,
    understating the honest max-pct — DEC-SWEEP-RESULTS-DEPTH-001 review
    NIT (a) fix). Full precision, never rounded (CLAUDE.md §4); a drop
    against a non-positive peak contributes ``0.0`` to the pct fold (no
    meaningful base to divide by).

    Honest envelope: intraday HIGHS are not tracked (the series carries
    adverse marks only), so a peak formed mid-session is sampled at that
    session's close — the figure is a lower bound on tick-path drawdown.
    """
    peak: int | None = start_equity_paisa
    if peak is not None and peak <= 0:
        raise MinutePathError(
            f"minute_path_max_drawdown: start_equity_paisa must be > 0, got {start_equity_paisa}"
        )
    max_dd = 0
    max_dd_pct = 0.0
    for point in points:
        trough = point.equity_close_paisa
        if point.equity_intraday_low_paisa is not None:
            trough = min(trough, point.equity_intraday_low_paisa)
        if peak is not None:
            drop = peak - trough
            max_dd = max(max_dd, drop)
            if peak > 0 and drop > 0:
                max_dd_pct = max(max_dd_pct, drop / peak)
        peak = point.equity_close_paisa if peak is None else max(peak, point.equity_close_paisa)
    return max_dd, max_dd_pct

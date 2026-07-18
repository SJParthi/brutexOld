"""1-minute-path math for the results-depth capture pass (DEC-SWEEP-RESULTS-DEPTH-001).

The sweep simulates trades at the tuple's OWN timeframe, so per-trade MFE/MAE
excursions are folded from sweep-TF bar highs/lows and the portfolio drawdown is
realized-equity-only. R22 demands the 1-MINUTE truth: MAE/MFE from the real
minute path — a gap-open minute far beyond the stop shows its TRUE adverse
depth — and intraday equity drawdown measured on the minute path.

**RUST-HOSTED LAW (DEC-RUST-CASCADE-RESULTS-001):** the numeric laws — the
span bisects, the excursion fold with the walker's exit-bar convention, the
per-session equity walk, the INDEPENDENT paisa/pct max-drawdown fold, the
IST session-date axis and the `rupees_to_paisa` price snap — live in
``rust/brutex-cascade/src/minute_path.rs`` (+ ``decimal.rs``/``ist.rs``),
verified against the FROZEN pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/minute_path.json``). This module
is the type face: the frozen record dataclasses, :class:`PathStatus`, the
:class:`EquityTradeLike` protocol and every repr-carrying validation raise;
prices cross the FFI as ``str(Decimal)`` (the exact-value marshalling law),
timestamps as exact epoch microseconds.

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
* IST attribution is the fixed +05:30 arithmetic of the crate's ``ist.rs`` —
  exactly ``ZoneInfo("Asia/Kolkata")`` for every post-1970 instant (the whole
  lake domain); every marshalled timestamp must be tz-aware (a naive
  timestamp raises ``TypeError`` at the epoch conversion — the models-layer
  tz-aware law, previously a silent local-tz guess).

## O() envelope

Building a :class:`MinuteIndex` is O(N_bars) once per (instrument, window)
(one price parse per bar); :meth:`MinuteIndex.span_slice` is O(log N); a
trade's excursion fold is O(minutes-in-trade); a session equity series is one
O(N_bars + N_trades) pass. Deterministic and pure: no globals, no wall-clock,
no RNG, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast

from brutex.engine.cascade_rust import EPOCH_DATE, epoch_day, epoch_us, require_cascade_rust
from brutex.engine.exit_index import bars_strictly_increasing
from brutex.models import BruteXError
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from decimal import Decimal

    from brutex_cascade import MinuteIndexRs

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


def _check_span(start: datetime, end: datetime) -> None:
    """The `span_slice` precondition — the ONE repr-carrying refusal."""
    if start > end:
        raise MinutePathError(f"span_slice: start {start!r} is after end {end!r}")


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
    _rs: object | None = field(default=None, compare=False, repr=False)

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
        timestamps = tuple(bar.timestamp for bar in frozen)
        return cls(bars=frozen, timestamps=timestamps, _rs=_build_rs_index(frozen, timestamps))

    def _rs_index(self) -> MinuteIndexRs:
        """The crate-side index (lazily built for a directly-constructed view)."""
        rs = self._rs
        if rs is None:
            rs = _build_rs_index(self.bars, self.timestamps)
            object.__setattr__(self, "_rs", rs)
        return cast("MinuteIndexRs", rs)

    def span_slice(self, start: datetime, end: datetime) -> tuple[int, int]:
        """Return ``(lo, hi)`` such that ``bars[lo:hi]`` are the bars in ``[start, end]``.

        Both endpoints INCLUSIVE; O(log N) bisect. An empty span returns
        ``lo == hi``.

        Raises
        ------
        MinutePathError
            If ``start > end``.
        """
        _check_span(start, end)
        lo, hi = self._rs_index().span_slice(epoch_us(start), epoch_us(end))
        return lo, hi


def _build_rs_index(bars: tuple[CandleBar, ...], timestamps: tuple[datetime, ...]) -> MinuteIndexRs:
    """Marshal the bar columns into the crate index (prices as ``str(Decimal)``)."""
    rs = require_cascade_rust()
    built = rs.MinuteIndexRs(
        [epoch_us(ts) for ts in timestamps],
        [str(bar.open) for bar in bars],
        [str(bar.high) for bar in bars],
        [str(bar.low) for bar in bars],
    )
    return cast("MinuteIndexRs", built)


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
    ``exit_bar_full_range = reason in {stop_loss, take_profit, end_of_data, session_gap}``
    (the reasons whose exit bar the walker itself folds in full).

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
    _check_span(entry_ts, exit_ts)
    mae, mfe, status = index._rs_index().excursions(
        epoch_us(entry_ts),
        epoch_us(exit_ts),
        entry_fill_paisa,
        direction is Side.BUY,
        exit_bar_full_range,
    )
    return TradePath1m(mae_paisa=mae, mfe_paisa=mfe, status=PathStatus(status))


class EquityTradeLike(Protocol):
    """Structural trade shape :func:`session_equity_series` consumes.

    Satisfied by ``brutex.engine.trade_capture.CapturedTrade`` (which cannot be
    imported here — ``trade_capture`` imports THIS module; the Protocol keeps
    the leaf cycle-free, the ``exit_index`` precedent).
    """

    @property
    def entry_ts(self) -> datetime:
        """The trade's entry instant (UTC, tz-aware)."""
        ...

    @property
    def exit_ts(self) -> datetime:
        """The trade's exit instant (UTC, tz-aware, ``>= entry_ts``)."""
        ...

    @property
    def direction(self) -> Side:
        """``Side.BUY`` or ``Side.SELL``."""
        ...

    @property
    def pnl_paisa(self) -> int:
        """The trade's realized P&L (signed int64 paisa)."""
        ...

    @property
    def entry_px(self) -> Decimal:
        """The exact entry fill price (2dp Decimal rupees)."""
        ...


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
    rs = require_cascade_rust()
    (day,) = rs.ist_session_dates([epoch_us(ts)])
    return EPOCH_DATE + timedelta(days=day)


def ist_session_dates(bars: Sequence[CandleBar]) -> tuple[date, ...]:
    """Return the sorted unique IST calendar dates of *bars* — the session axis.

    The window's session list for :func:`session_equity_series`, derived from
    the SAME bars the sweep loaded (dates with data, never a fabricated
    calendar). One O(N_bars) pass.
    """
    rs = require_cascade_rust()
    days = rs.ist_session_dates([epoch_us(bar.timestamp) for bar in bars])
    return tuple(EPOCH_DATE + timedelta(days=day) for day in days)


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

    rs = require_cascade_rust()
    rs_index = None
    if index is not None and len(index.bars) > 0:
        rs_index = index._rs_index()
    raw_points, status = rs.session_equity_series(
        rs_index,
        [epoch_us(t.entry_ts) for t in trades],
        [epoch_us(t.exit_ts) for t in trades],
        [t.direction is Side.BUY for t in trades],
        [t.pnl_paisa for t in trades],
        [str(t.entry_px) for t in trades],
        [epoch_day(day) for day in sessions],
        start_equity_paisa,
    )
    points = tuple(
        SessionEquityPoint(
            session_date=day,
            equity_close_paisa=close,
            equity_intraday_low_paisa=low,
            trades_closed=closed,
        )
        for day, (close, low, closed) in zip(sessions, raw_points, strict=True)
    )
    return points, PathStatus(status)


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
    if start_equity_paisa is not None and start_equity_paisa <= 0:
        raise MinutePathError(
            f"minute_path_max_drawdown: start_equity_paisa must be > 0, got {start_equity_paisa}"
        )
    rs = require_cascade_rust()
    dd, pct = rs.minute_path_max_drawdown(
        [point.equity_close_paisa for point in points],
        [point.equity_intraday_low_paisa for point in points],
        start_equity_paisa,
    )
    return dd, pct

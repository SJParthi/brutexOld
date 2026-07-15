"""Per-finalist trade capture — the oracle re-sim behind the results-depth pass.

DEC-SWEEP-RESULTS-DEPTH-001 (WP1). The production sweep discards every trade
list by design (R30's SHRINK law — losers never touch disk), so the R24
every-trade-visible audit trail is built POST-HOC: this module re-simulates ONE
persisted finalist (:class:`~brutex.engine.results_store.ResultRow`) on the
pure-Python oracle — the exact ``ranked_combo_to_result_row`` precedent, "once
per top-K FINALIST (low volume)" — and captures what the hot path throws away:

* the exit reason + exact fill prices, via the additive
  :class:`~brutex.engine.trade_sim.TradeDetail` sink;
* the India VIX stamp (``vix_entry`` / ``vix_exit``) via the previously-dormant
  :class:`~brutex.engine.reference_series.TradeReferenceStamper` (R31 — a
  missing VIX bar is an honest ``None``, counted, never a silent zero);
* the 1-MINUTE-path MAE/MFE + the per-session minute-path equity series (R22),
  via :mod:`brutex.engine.minute_path`.

## The parity tooth (correctness bridge)

The stored row came off the Rust kernel arm; the captured trades come off the
Python oracle. Kernel⇄oracle bit-parity is golden-proven upstream, and this
module converts any residual drift into a LOUD, counted refusal: the recomputed
``total_trades / winning_trades / losing_trades / net_pnl_paisa`` must equal
the stored row's, else ONE ``results_depth.capture_mismatch`` WARN fires and
the capture returns ``parity_ok=False`` with an EMPTY trade list — a reason can
never be attached to a trade list that does not match the stored result
(design F.3/F.4), and the pass counts + skips the combo, never silently.

## NULL-by-design lots + costs (gap-register Ruling 18, 2026-07-08)

``lots`` and ``costs_paisa`` are honest ``None``/NULL on every captured trade
until P5's futures execution-translation sidecar lands: the signal-only index
sweep has NO position sizing and NO cost stack, and the earlier fabricated
``lots=1`` / ``costs_paisa=0`` constants are REMOVED by coordinator ruling —
"never fabricated constants". The wire carries the ``None`` explicitly so a
consumer never mistakes an unsized signal leg for a 1-lot execution; nothing
downstream may multiply by a fabricated lot (the margin-ROI surface is
independently all-UNVERIFIED).

## O() envelope

One oracle walk (O(N_bars)) + one O(minutes-per-trade) 1m fold per trade + one
O(N_1m_bars) equity pass — per FINALIST, run once at run-end. Never on the
~33B-trial hot path (which receives ZERO diffs from this package).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.minute_path import (
    MinuteIndex,
    PathStatus,
    SessionEquityPoint,
    TradePath1m,
    ist_session_dates,
    session_equity_series,
    trade_excursions_1m,
)
from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA
from brutex.engine.trade_sim import EXIT_REASONS, FillPolicy, TradeDetail, simulate_trades
from brutex.execution.fill_mode import FillMode
from brutex.models import BruteXError
from brutex.money import paisa_to_rupees
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.engine.results_store import ResultRow
    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger("brutex.engine.trade_capture")

__all__ = [
    "CapturedTrade",
    "ComboCapture",
    "TradeCaptureError",
    "capture_combo",
]

#: Exit reasons whose EXIT bar the walker folds in full (level fills at
#: ``AT_LEVEL`` + the end-of-data close + the session-boundary ``session_gap``
#: close happen WITHIN the exit bar's range — a ``session_gap`` fill is the
#: CLOSE of a fully-folded bar, mechanically identical to ``end_of_data``,
#: DEC-TRADESIM-OPEN-SESSION-GUARD-001); ``signal`` / ``forced_1520`` fill at
#: the exit bar's OPEN, so only the open is folded — the 1m path mirrors the
#: walker convention exactly
#: (:func:`brutex.engine.minute_path.trade_excursions_1m`).
_FULL_RANGE_EXIT_REASONS: Final[frozenset[str]] = frozenset(
    {"stop_loss", "take_profit", "end_of_data", "session_gap"}
)

#: The parity-tooth field names, in report order (recomputed vs stored row).
_PARITY_FIELDS: Final[tuple[str, ...]] = (
    "total_trades",
    "winning_trades",
    "losing_trades",
    "net_pnl_paisa",
)


class TradeCaptureError(BruteXError):
    """Raised on a broken capture invariant (fill-belt / alignment violations)."""


@dataclass(frozen=True, slots=True)
class CapturedTrade:
    """One captured trade — the binding P3→P4 trade-row contract, field-for-field.

    Timestamps tz-aware UTC; prices exact 2dp Decimal rupees (derived from the
    walker's paisa fills — never re-rounded floats); money int64 paisa; VIX
    full-precision Decimal or an honest ``None``. The 1-minute-path pair is
    ``None`` exactly when ``path_1m_status is PathStatus.NO_1M_DATA``.
    ``lots`` / ``costs_paisa`` are NULL-BY-DESIGN ``None`` until P5's futures
    translation sidecar lands (gap-register Ruling 18 — never a fabricated
    ``1`` / ``0``); a future non-``None`` value must be a REAL execution-leg
    figure (``lots >= 1``, ``costs_paisa >= 0``).
    """

    entry_ts: datetime
    entry_px: Decimal
    exit_ts: datetime
    exit_px: Decimal
    direction: Side
    lots: int | None
    exit_reason: str
    pnl_paisa: int
    costs_paisa: int | None
    mae_paisa: int
    mfe_paisa: int
    mae_1m_paisa: int | None
    mfe_1m_paisa: int | None
    path_1m_status: PathStatus
    vix_entry: Decimal | None
    vix_exit: Decimal | None

    def __post_init__(self) -> None:
        if self.direction is Side.HOLD:
            raise TradeCaptureError("CapturedTrade.direction must be BUY or SELL, not HOLD")
        if self.exit_reason not in EXIT_REASONS:
            raise TradeCaptureError(
                f"CapturedTrade.exit_reason {self.exit_reason!r} is not one of {EXIT_REASONS}"
            )
        if self.lots is not None and self.lots < 1:
            raise TradeCaptureError(f"CapturedTrade.lots must be None or >= 1, got {self.lots}")
        if self.costs_paisa is not None and self.costs_paisa < 0:
            raise TradeCaptureError(
                f"CapturedTrade.costs_paisa must be None or >= 0, got {self.costs_paisa}"
            )
        if self.mae_paisa < 0 or self.mfe_paisa < 0:
            raise TradeCaptureError(
                "CapturedTrade.mae_paisa/mfe_paisa are magnitudes and must be >= 0, "
                f"got mae={self.mae_paisa} mfe={self.mfe_paisa}"
            )
        no_path = self.path_1m_status is PathStatus.NO_1M_DATA
        if (self.mae_1m_paisa is None) != no_path or (self.mfe_1m_paisa is None) != no_path:
            raise TradeCaptureError(
                "CapturedTrade: mae_1m_paisa/mfe_1m_paisa must be None exactly when "
                f"path_1m_status is NO_1M_DATA (got status={self.path_1m_status.value}, "
                f"mae_1m={self.mae_1m_paisa}, mfe_1m={self.mfe_1m_paisa})"
            )
        if (self.mae_1m_paisa is not None and self.mae_1m_paisa < 0) or (
            self.mfe_1m_paisa is not None and self.mfe_1m_paisa < 0
        ):
            raise TradeCaptureError(
                "CapturedTrade: 1m excursions are magnitudes and must be >= 0, "
                f"got mae_1m={self.mae_1m_paisa} mfe_1m={self.mfe_1m_paisa}"
            )


@dataclass(frozen=True, slots=True)
class ComboCapture:
    """One finalist's full capture — trades, equity series, honesty counters.

    ``parity_ok=False`` means the oracle re-sim did NOT reproduce the stored
    row's headline counts: ``trades`` and ``equity`` are then EMPTY (a wrong
    trade list is never exposed) and ``parity_detail`` names every differing
    field, recomputed-vs-stored. ``vix_missing_trades`` counts trades whose
    entry OR exit VIX stamp is ``None`` (a ``None`` stamper misses them all).
    """

    run_id: str
    trades: tuple[CapturedTrade, ...]
    equity: tuple[SessionEquityPoint, ...]
    equity_status: PathStatus
    vix_missing_trades: int
    parity_ok: bool
    parity_detail: str


def derived_exit_fill(direction: Side, entry_fill_paisa: int, pnl_paisa: int) -> int:
    """Re-derive the exit fill from the entry fill + signed P&L (the belt input).

    Inverts the walker's ``_close`` arithmetic exactly: ``exit = entry + pnl``
    for BUY, ``entry − pnl`` for SELL. ``HOLD`` is rejected.
    """
    if direction is Side.HOLD:
        raise TradeCaptureError("derived_exit_fill: direction must be BUY or SELL, not HOLD")
    if direction is Side.BUY:
        return entry_fill_paisa + pnl_paisa
    return entry_fill_paisa - pnl_paisa


def _check_detail_alignment(n_trades: int, n_details: int) -> None:
    """Belt: the detail sink must carry EXACTLY one record per closed trade."""
    if n_trades != n_details:
        raise TradeCaptureError(
            f"capture_combo: detail sink carries {n_details} record(s) for "
            f"{n_trades} trade(s) — the walker's per-close append invariant is broken"
        )


def _check_exit_fill_belt(detail: TradeDetail, direction: Side) -> None:
    """Belt: the recorded exit fill must equal the P&L-derived one (bit-exact)."""
    derived = derived_exit_fill(direction, detail.entry_fill_paisa, detail.trade.pnl_paisa)
    if derived != detail.exit_fill_paisa:
        raise TradeCaptureError(
            f"capture_combo: exit-fill belt failed — recorded {detail.exit_fill_paisa} "
            f"!= derived {derived} (entry {detail.entry_fill_paisa} "
            f"pnl {detail.trade.pnl_paisa} {direction.value})"
        )


def _parity_detail(row: ResultRow, trades: Sequence[ClosedTrade]) -> str:
    """Return '' when the recomputed headline counts equal the stored row's.

    A non-empty return names every differing field as
    ``field=recomputed!=stored`` — the loud, greppable mismatch report.
    """
    recomputed = {
        "total_trades": len(trades),
        "winning_trades": sum(1 for t in trades if t.pnl_paisa > 0),
        "losing_trades": sum(1 for t in trades if t.pnl_paisa < 0),
        "net_pnl_paisa": sum(t.pnl_paisa for t in trades),
    }
    diffs = [
        f"{name}={recomputed[name]}!={getattr(row, name)}"
        for name in _PARITY_FIELDS
        if recomputed[name] != getattr(row, name)
    ]
    return ", ".join(diffs)


def capture_combo(
    row: ResultRow,
    *,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
    minute_index: MinuteIndex | None,
    stamper: TradeReferenceStamper | None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    fill_mode: FillMode = FillMode.PRECISE,
) -> ComboCapture:
    """Re-simulate one stored finalist and capture its full trade depth.

    Parameters
    ----------
    row
        The persisted :class:`~brutex.engine.results_store.ResultRow` (the
        parity oracle — its ``mask_bits`` / ``direction`` drive the re-sim).
    bars, bar_bits
        The SAME sweep-TF bars + Stage-1A matrix the sweep used (rebuilt once
        per tuple by the run-end pass; ``bar_bits`` reused, never re-derived
        per combo).
    tf_minutes, sl_paisa, tp_paisa
        The tuple's trade-sim knobs — must match the sweep's so the oracle
        reproduces the stored trades exactly (the
        ``ranked_combo_to_result_row`` contract).
    fill_mode
        The run's :class:`~brutex.execution.fill_mode.FillMode`
        (DEC-FILL-WORST-CASE-001) — the PARITY TOOTH's fourth knob: a
        WORST_CASE store re-captured PRECISE would fail parity on every
        combo, so the caller threads it from the SAME grid config it
        sources sl/tp from (never independently from env). Default PRECISE
        keeps legacy callers byte-identical.
    minute_index
        The instrument's 1-minute :class:`~brutex.engine.minute_path.MinuteIndex`
        for the window, or ``None`` when no 1m data exists — every trade then
        carries the honest ``NO_1M_DATA`` status (never fabricated).
    stamper
        The India VIX :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (this pass is its first production construction site — R31), or
        ``None`` when no VIX bars exist: every trade's VIX is then ``None``
        and counted in ``vix_missing_trades``.
    start_equity_paisa
        The equity-series base (the sweep's own constant by default).

    Returns
    -------
    ComboCapture
        The captured trades + per-session equity, or the loud empty
        ``parity_ok=False`` capture on a kernel⇄oracle drift (WARNed as
        ``results_depth.capture_mismatch``, never silent).

    Raises
    ------
    TradeCaptureError
        On a broken internal invariant (detail-sink misalignment / exit-fill
        belt) — a genuine code defect, surfaced loudly to the pass's fence.
    """
    side = Side.BUY if row.direction == "bull" else Side.SELL
    detail_sink: list[TradeDetail] = []
    trades = simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, row.mask_bits),
        direction=side,
        tf_minutes=tf_minutes,
        stop_loss_paisa=sl_paisa,
        take_profit_paisa=tp_paisa,
        policy=FillPolicy(fill_mode=fill_mode),
        reference_stamper=stamper,
        detail_sink=detail_sink,
    )
    parity = _parity_detail(row, trades)
    if parity:
        _LOG.warning(
            "results_depth.capture_mismatch",
            run_id=row.run_id,
            instrument=row.instrument,
            tf=row.tf,
            direction=row.direction,
            detail=parity,
        )
        return ComboCapture(
            run_id=row.run_id,
            trades=(),
            equity=(),
            equity_status=PathStatus.NO_1M_DATA,
            vix_missing_trades=0,
            parity_ok=False,
            parity_detail=parity,
        )
    _check_detail_alignment(len(trades), len(detail_sink))

    captured: list[CapturedTrade] = []
    vix_missing = 0
    for detail in detail_sink:
        _check_exit_fill_belt(detail, side)
        trade = detail.trade
        if minute_index is not None:
            path = trade_excursions_1m(
                minute_index,
                entry_ts=trade.entry_ts,
                exit_ts=trade.exit_ts,
                entry_fill_paisa=detail.entry_fill_paisa,
                direction=side,
                exit_bar_full_range=detail.exit_reason in _FULL_RANGE_EXIT_REASONS,
            )
        else:
            path = TradePath1m(mae_paisa=None, mfe_paisa=None, status=PathStatus.NO_1M_DATA)
        if trade.vix_at_entry is None or trade.vix_at_exit is None:
            vix_missing += 1
        captured.append(
            CapturedTrade(
                entry_ts=trade.entry_ts,
                entry_px=paisa_to_rupees(detail.entry_fill_paisa),
                exit_ts=trade.exit_ts,
                exit_px=paisa_to_rupees(detail.exit_fill_paisa),
                direction=side,
                lots=None,  # NULL-by-design until P5 (Ruling 18 — never a fabricated 1)
                exit_reason=detail.exit_reason,
                pnl_paisa=trade.pnl_paisa,
                costs_paisa=None,  # NULL-by-design until P5 (never a fabricated 0)
                mae_paisa=trade.mae_paisa,
                mfe_paisa=trade.mfe_paisa,
                mae_1m_paisa=path.mae_paisa,
                mfe_1m_paisa=path.mfe_paisa,
                path_1m_status=path.status,
                vix_entry=trade.vix_at_entry,
                vix_exit=trade.vix_at_exit,
            )
        )

    equity, equity_status = session_equity_series(
        minute_index,
        captured,
        sessions=ist_session_dates(bars),
        start_equity_paisa=start_equity_paisa,
    )
    return ComboCapture(
        run_id=row.run_id,
        trades=tuple(captured),
        equity=equity,
        equity_status=equity_status,
        vix_missing_trades=vix_missing,
        parity_ok=True,
        parity_detail="",
    )

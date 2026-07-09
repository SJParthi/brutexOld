"""Per-combo aggregate record for the results-depth pass (R22/R31/R46/R51).

DEC-SWEEP-RESULTS-DEPTH-001 (WP2). :func:`aggregate_combo` folds ONE finalist's
:class:`~brutex.engine.trade_capture.ComboCapture` + the tuple's
:class:`~brutex.engine.session_lens.SessionLens` into ONE
:class:`ComboAggregate` — the record persisted to ``<stem>.aggs.parquet``
(schema authority: :data:`brutex.engine.trade_store.AGG_COLUMNS`; a test pins
``ComboAggregate.to_record()`` keys against it so the two can never drift).

## Field → computed-source traceability (R27 "no invented text anywhere")

Every field's exact computed source is documented on the field itself (see
:class:`ComboAggregate`). Summary of the laws used:

* Integer paisa averages use FLOOR division (``sum // n`` — the
  ``ResultMetrics.avg_mae_paisa`` law in :mod:`brutex.metrics.compute`).
* Percentiles are deterministic NEAREST-RANK on the sorted values with
  integer-exact rank arithmetic (``rank = ceil(pct·n/100)`` computed as
  ``-(-pct·n // 100)`` — no float, no interpolation).
* Statistical ratios (VIX stats, ``win_loss_ratio``, ``recovery_factor``,
  ``composite_score``, ``max_dd_minute_path_pct``) are FULL-precision floats,
  NEVER rounded (CLAUDE.md §4).
* ``win_loss_ratio`` mirrors :mod:`brutex.metrics.compute` exactly:
  ``avg_win = gross_profit // winners``, ``avg_loss = |gross_loss| // losers``,
  ratio ``None`` when there is no loser (no real denominator).
* ``recovery_factor`` reapplies the compute-module law to the STORED row:
  ``row.net_pnl_paisa / row.max_drawdown_paisa``, ``None`` when the stored
  drawdown is zero.

## Honesty envelope (Golden Rule #1 / #10)

* A ZERO-TRADE combo (Stage-3 ungated row) and a PARITY-FAILED capture both
  produce a first-class NULL record: every nullable statistic ``None``, every
  JSON lens empty, every tally zero — never a fabricated number, never a
  divide-by-zero. ``zero_trades`` reflects the STORED row's truth
  (``row.total_trades == 0``); ``parity_ok`` reflects the capture's verdict.
* 1-minute-path aggregates are ``None`` unless EVERY counted trade's minute
  path was fully covered (``PathStatus.OK``) — partial coverage never averages
  apples with oranges; coverage is exposed as ``trades_with_1m``.
* VIX stats use each trade's ``vix_entry`` stamp (the level at the decision
  instant); an unstamped trade is EXCLUDED and counted — zero stamped winners
  ⇒ a ``None`` band, which P4 renders as the honest "NO VIX DATA".
* The one-regime-wonder rule is the PINNED constant
  :data:`REGIME_WONDER_SHARE` (``0.8``): flagged iff the combo's net P&L is
  positive AND one regime holds ≥ 80% of the sum of positive regime P&Ls,
  evaluated over calendar years and over trend regimes (the ``unknown``
  trend bucket is excluded from the rule — we never claim regime knowledge
  the lens lacks). Both axes firing reports ``"year"`` (pinned precedence).
* The behavioral-twin ``fingerprint`` is DELEGATED: the caller computes it
  via ``brutex.engine.twin_detect.trade_fingerprint`` (WP4) and passes it in
  — delegation-by-parameter keeps this module standalone while twin grouping
  stays ONE-sourced (design A.5/A.6).

## O() envelope

One O(n_trades · log n_trades) fold per finalist (sorting for percentiles
dominates) + one O(n_sessions) drawdown pass — run once at run-end per
persisted finalist, never on the hot path. Pure and deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from brutex.engine.minute_path import PathStatus, minute_path_max_drawdown
from brutex.engine.session_lens import entry_hour_ist, ist_trade_date
from brutex.engine.trade_sim import EXIT_REASONS
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Iterable, Sequence

    from brutex.engine.results_store import ResultRow
    from brutex.engine.session_lens import SessionLens
    from brutex.engine.trade_capture import CapturedTrade, ComboCapture

__all__ = [
    "REGIME_WONDER_SHARE",
    "TREND_UNKNOWN",
    "WORST_DAYS_LIMIT",
    "ComboAggregate",
    "ComboAggregateError",
    "aggregate_combo",
    "nearest_rank",
]

#: The pinned one-regime-wonder threshold (R46): one regime holding at least
#: this share of the sum of POSITIVE regime P&Ls flags the combo (given a
#: positive net P&L). A deliberate display heuristic — documented, never a
#: ranking gate.
REGIME_WONDER_SHARE: Final[float] = 0.8

#: The worst-days stress view depth (R22 "explicit worst-5-days stress view").
WORST_DAYS_LIMIT: Final[int] = 5

#: The trend bucket for a trade whose entry date the lens cannot label (a
#: session outside the lens's daily-bar coverage) — an honest bucket, never a
#: silent drop and never a guessed regime.
TREND_UNKNOWN: Final[str] = "unknown"

#: The regime-wonder axes in pinned precedence order (design B2).
_AXIS_YEAR: Final[str] = "year"
_AXIS_TREND: Final[str] = "trend"

#: Integer percent ranks used by the nearest-rank percentiles.
_PCT_P95: Final[int] = 95
_PCT_P10: Final[int] = 10
_PCT_P90: Final[int] = 90
_PCT_MAX: Final[int] = 100


class ComboAggregateError(BruteXError):
    """Raised on a broken aggregate invariant (mismatched join, bad tallies)."""


def nearest_rank(sorted_values: Sequence[int] | Sequence[float], pct: int) -> int | float:
    """Return the nearest-rank *pct*-th percentile of pre-sorted values.

    The design-pinned deterministic law: ``rank = ceil(pct · n / 100)`` (with
    EXACT integer arithmetic — ``-(-pct · n // 100)`` — so no float rounding
    can ever move a rank), value = ``sorted_values[rank - 1]``. No
    interpolation: the returned value is always ONE of the inputs
    (integer-exact for paisa lists).

    Raises
    ------
    ComboAggregateError
        If *sorted_values* is empty or *pct* is outside ``[1, 100]``.
    """
    n = len(sorted_values)
    if n == 0:
        raise ComboAggregateError("nearest_rank: values must be non-empty")
    if not 1 <= pct <= _PCT_MAX:
        raise ComboAggregateError(f"nearest_rank: pct must be in [1, 100], got {pct}")
    rank = -(-pct * n // _PCT_MAX)
    return sorted_values[rank - 1]


def _dumps(payload: object) -> str:
    """The ONE deterministic JSON rendering law (B2): sorted keys, compact,
    ASCII, NaN refused loudly."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class ComboAggregate:
    """One finalist's aggregate record — one row of ``<stem>.aggs.parquet``.

    Field-for-field the :data:`brutex.engine.trade_store.AGG_COLUMNS` schema
    (test-pinned). Each field states its computed source (R27 traceability):

    * ``run_id`` — the finalist's stored run identity (join key).
    * ``n_trades`` — ``len(capture.trades)`` (the ORACLE re-sim's captured
      trades; equals the stored ``total_trades`` when ``parity_ok``).
    * ``zero_trades`` — ``row.total_trades == 0`` (the store's truth).
    * ``parity_ok`` — the capture's kernel⇄oracle parity verdict; ``False``
      ⇒ trades were withheld and every statistic below is ``None``/empty.
    * ``mae_avg/worst/p95_paisa`` — floor-mean / max / nearest-rank-p95 of
      the captured trades' sweep-TF ``mae_paisa`` magnitudes.
    * ``mfe_avg/best/p95_paisa`` — same folds over ``mfe_paisa``.
    * ``mae_1m_*`` / ``mfe_1m_*`` — the same folds over the 1-MINUTE-path
      excursions, ``None`` unless EVERY counted trade has
      ``path_1m_status == "ok"`` (partial coverage never averaged).
    * ``trades_with_1m`` — count of trades with a fully-covered minute path.
    * ``max_dd_minute_path_paisa/pct`` — R22 intraday-path max drawdown via
      :func:`brutex.engine.minute_path.minute_path_max_drawdown` over the
      capture's per-session equity series (peak-anchored at the sim's
      starting equity); ``pct`` full precision and tracked INDEPENDENTLY of
      the paisa maximum — it is the honest MAX percentage drawdown, which
      may occur at a DIFFERENT point than the absolute-paisa maximum (a
      smaller drop from a smaller earlier peak can be the larger share).
    * ``equity_status`` — the equity series' minute-coverage honesty verdict
      (``ok`` / ``partial`` / ``no_1m_data``).
    * ``worst5_days_json`` — ``[[iso_date, pnl_paisa], …]`` (≤ 5): the
      lowest per-session P&L sums over the trades' exit IST dates, most
      negative first (ties by date ascending) — R22's stress view.
    * ``vix_win_min/avg/max`` / ``vix_loss_min/avg/max`` — min/mean/max of
      the stamped ``vix_entry`` over winning (``pnl > 0``) / losing
      (``pnl < 0``) trades separately; full-precision floats; ``None`` when
      no stamped trade in that class.
    * ``vix_band_lo/hi`` — R31's "works when VIX between X-Y" band =
      nearest-rank [p10, p90] of the stamped WINNING trades' ``vix_entry``;
      ``None`` when zero stamped winners (P4 renders "NO VIX DATA").
    * ``vix_stamped_trades`` — trades carrying a ``vix_entry`` stamp.
    * ``vix_missing_trades`` — the capture's count of trades missing an
      entry OR exit stamp (honest NULL counter, R31).
    * ``pnl_by_year_json`` — ``{year: Σ pnl_paisa}`` over exit IST dates
      (realized-on-exit attribution; sums exactly to the net P&L).
    * ``pnl_by_trend_json`` — ``{regime: Σ pnl_paisa}`` over the lens's
      trend label at each trade's ENTRY IST date (the regime the trade was
      taken in; intraday law ⇒ entry date == exit date); an unlabelled date
      buckets under ``"unknown"``.
    * ``one_regime_wonder`` / ``regime_wonder_axis`` — the pinned
      :data:`REGIME_WONDER_SHARE` rule over the year axis then the trend
      axis (module docstring).
    * ``entry_hour_hist_json`` — ``{ist_hour: count}`` over entry
      timestamps (R51).
    * ``event_day_counts_json`` / ``event_day_pnl_json`` — per verified
      event flag on the trade's entry date: trade count / Σ pnl (R51).
    * ``exits_*`` — exit-reason tallies over the captured trades' walker
      reasons (they always sum to ``n_trades``).
    * ``max_consecutive_losses/wins`` — longest run fold over the
      chronological trades (a scratch ``pnl == 0`` breaks BOTH runs — the
      ``metrics.compute._consecutive_runs`` law, re-implemented as the
      public :func:`_consecutive_runs` twin here).
    * ``composite_score`` — caller-supplied (the COMMON
      ``ranking.composite_scores`` value computed tuple-wide by the pass);
      full precision, ``None`` when not computed.
    * ``win_loss_ratio`` — the compute-module law over the captured trades.
    * ``recovery_factor`` — ``row.net_pnl_paisa / row.max_drawdown_paisa``
      (``None`` when the stored drawdown is 0).
    * ``fingerprint`` — the behavioral-twin trade-list fingerprint,
      caller-supplied from ``twin_detect.trade_fingerprint`` (WP4).
    """

    run_id: str
    n_trades: int
    zero_trades: bool
    parity_ok: bool
    mae_avg_paisa: int | None
    mae_worst_paisa: int | None
    mae_p95_paisa: int | None
    mfe_avg_paisa: int | None
    mfe_best_paisa: int | None
    mfe_p95_paisa: int | None
    mae_1m_avg_paisa: int | None
    mae_1m_worst_paisa: int | None
    mae_1m_p95_paisa: int | None
    mfe_1m_avg_paisa: int | None
    mfe_1m_best_paisa: int | None
    mfe_1m_p95_paisa: int | None
    trades_with_1m: int
    max_dd_minute_path_paisa: int | None
    max_dd_minute_path_pct: float | None
    equity_status: str
    worst5_days_json: str
    vix_win_min: float | None
    vix_win_avg: float | None
    vix_win_max: float | None
    vix_loss_min: float | None
    vix_loss_avg: float | None
    vix_loss_max: float | None
    vix_band_lo: float | None
    vix_band_hi: float | None
    vix_stamped_trades: int
    vix_missing_trades: int
    pnl_by_year_json: str
    pnl_by_trend_json: str
    one_regime_wonder: bool
    regime_wonder_axis: str | None
    entry_hour_hist_json: str
    event_day_counts_json: str
    event_day_pnl_json: str
    exits_stop_loss: int
    exits_take_profit: int
    exits_signal: int
    exits_forced_1520: int
    exits_end_of_data: int
    max_consecutive_losses: int
    max_consecutive_wins: int
    composite_score: float | None
    win_loss_ratio: float | None
    recovery_factor: float | None
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ComboAggregateError("ComboAggregate.run_id must be non-empty")
        if not self.fingerprint:
            raise ComboAggregateError("ComboAggregate.fingerprint must be non-empty")
        if self.n_trades < 0:
            raise ComboAggregateError(f"ComboAggregate.n_trades must be >= 0, got {self.n_trades}")
        if not 0 <= self.trades_with_1m <= self.n_trades:
            raise ComboAggregateError(
                f"ComboAggregate.trades_with_1m must be in [0, n_trades], got "
                f"{self.trades_with_1m} of {self.n_trades}"
            )
        if self.vix_stamped_trades < 0 or self.vix_missing_trades < 0:
            raise ComboAggregateError(
                "ComboAggregate VIX counters must be >= 0, got "
                f"stamped={self.vix_stamped_trades} missing={self.vix_missing_trades}"
            )
        exits = (
            self.exits_stop_loss,
            self.exits_take_profit,
            self.exits_signal,
            self.exits_forced_1520,
            self.exits_end_of_data,
        )
        if any(count < 0 for count in exits) or sum(exits) != self.n_trades:
            raise ComboAggregateError(
                f"ComboAggregate exit tallies {exits} must be >= 0 and sum to "
                f"n_trades={self.n_trades}"
            )

    def to_record(self) -> dict[str, object]:
        """Render the ``AGG_COLUMNS``-keyed record ``write_tuple_sidecars`` consumes.

        Key set + order mirror :data:`brutex.engine.trade_store.AGG_COLUMNS`
        exactly (test-pinned; drift on either side fails the pin loudly).
        """
        return {
            "run_id": self.run_id,
            "n_trades": self.n_trades,
            "zero_trades": self.zero_trades,
            "parity_ok": self.parity_ok,
            "mae_avg_paisa": self.mae_avg_paisa,
            "mae_worst_paisa": self.mae_worst_paisa,
            "mae_p95_paisa": self.mae_p95_paisa,
            "mfe_avg_paisa": self.mfe_avg_paisa,
            "mfe_best_paisa": self.mfe_best_paisa,
            "mfe_p95_paisa": self.mfe_p95_paisa,
            "mae_1m_avg_paisa": self.mae_1m_avg_paisa,
            "mae_1m_worst_paisa": self.mae_1m_worst_paisa,
            "mae_1m_p95_paisa": self.mae_1m_p95_paisa,
            "mfe_1m_avg_paisa": self.mfe_1m_avg_paisa,
            "mfe_1m_best_paisa": self.mfe_1m_best_paisa,
            "mfe_1m_p95_paisa": self.mfe_1m_p95_paisa,
            "trades_with_1m": self.trades_with_1m,
            "max_dd_minute_path_paisa": self.max_dd_minute_path_paisa,
            "max_dd_minute_path_pct": self.max_dd_minute_path_pct,
            "equity_status": self.equity_status,
            "worst5_days_json": self.worst5_days_json,
            "vix_win_min": self.vix_win_min,
            "vix_win_avg": self.vix_win_avg,
            "vix_win_max": self.vix_win_max,
            "vix_loss_min": self.vix_loss_min,
            "vix_loss_avg": self.vix_loss_avg,
            "vix_loss_max": self.vix_loss_max,
            "vix_band_lo": self.vix_band_lo,
            "vix_band_hi": self.vix_band_hi,
            "vix_stamped_trades": self.vix_stamped_trades,
            "vix_missing_trades": self.vix_missing_trades,
            "pnl_by_year_json": self.pnl_by_year_json,
            "pnl_by_trend_json": self.pnl_by_trend_json,
            "one_regime_wonder": self.one_regime_wonder,
            "regime_wonder_axis": self.regime_wonder_axis,
            "entry_hour_hist_json": self.entry_hour_hist_json,
            "event_day_counts_json": self.event_day_counts_json,
            "event_day_pnl_json": self.event_day_pnl_json,
            "exits_stop_loss": self.exits_stop_loss,
            "exits_take_profit": self.exits_take_profit,
            "exits_signal": self.exits_signal,
            "exits_forced_1520": self.exits_forced_1520,
            "exits_end_of_data": self.exits_end_of_data,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_consecutive_wins": self.max_consecutive_wins,
            "composite_score": self.composite_score,
            "win_loss_ratio": self.win_loss_ratio,
            "recovery_factor": self.recovery_factor,
            "fingerprint": self.fingerprint,
        }


def _consecutive_runs(pnls: Sequence[int]) -> tuple[int, int]:
    """Longest losing / winning streaks over chronological per-trade P&Ls.

    The public twin of ``brutex.metrics.compute._consecutive_runs`` (same law,
    re-implemented so no private is imported): a loss (``pnl < 0``) extends
    the losing run and breaks the winning one; a win the mirror; a scratch
    (``pnl == 0``) breaks BOTH.
    """
    max_losses = max_wins = cur_losses = cur_wins = 0
    for pnl in pnls:
        if pnl < 0:
            cur_losses += 1
            cur_wins = 0
        elif pnl > 0:
            cur_wins += 1
            cur_losses = 0
        else:
            cur_losses = 0
            cur_wins = 0
        max_losses = max(max_losses, cur_losses)
        max_wins = max(max_wins, cur_wins)
    return max_losses, max_wins


def _int_stats(values: Sequence[int]) -> tuple[int, int, int]:
    """``(floor_mean, max, nearest_rank_p95)`` over non-empty int magnitudes."""
    ordered = sorted(values)
    return sum(ordered) // len(ordered), ordered[-1], cast(int, nearest_rank(ordered, _PCT_P95))


def _float_stats(values: Sequence[float]) -> tuple[float, float, float]:
    """``(min, mean, max)`` over non-empty full-precision floats.

    The mean is CLAMPED into the ``[min, max]`` bracket: IEEE-754 summation
    can push the raw ``sum/n`` a few ULP outside it — e.g. three trades all
    stamped at the same 2-dp India-VIX print ``6.15`` sum to
    ``18.450000000000003`` whose third is ``6.150000000000001 > 6.15``
    (six such trades land BELOW the min instead) — which would persist an
    internally-inconsistent ``avg > max`` / ``avg < min`` on the combo card
    and break the documented ``min ≤ mean ≤ max`` invariant. The band edges
    are always REAL input values, so only the mean needs the clamp; a mean
    already inside the bracket is returned untouched (full precision — this
    is an ULP-artifact correction, never a rounding).
    """
    lo = min(values)
    hi = max(values)
    return lo, min(max(sum(values) / len(values), lo), hi), hi


def _one_regime_dominates(pnls: Iterable[int]) -> bool:
    """The pinned share rule: max positive regime P&L ≥ 80% of Σ positives."""
    positives = [value for value in pnls if value > 0]
    if not positives:
        return False
    return max(positives) / sum(positives) >= REGIME_WONDER_SHARE


def _worst_days_json(trades: Sequence[CapturedTrade]) -> str:
    """R22 worst-days view: the ≤5 lowest per-session P&L sums, date-tagged."""
    by_day: dict[str, int] = {}
    for trade in trades:
        key = ist_trade_date(trade.exit_ts).isoformat()
        by_day[key] = by_day.get(key, 0) + trade.pnl_paisa
    worst = sorted(by_day.items(), key=lambda item: (item[1], item[0]))[:WORST_DAYS_LIMIT]
    return _dumps([[day, pnl] for day, pnl in worst])


def aggregate_combo(
    capture: ComboCapture,
    row: ResultRow,
    *,
    lens: SessionLens,
    composite_score: float | None,
    fingerprint: str,
    start_equity_paisa: int,
) -> ComboAggregate:
    """Fold one finalist's capture + the tuple lens into its aggregate record.

    Parameters
    ----------
    capture
        The finalist's :class:`~brutex.engine.trade_capture.ComboCapture`
        (oracle re-sim). A ``parity_ok=False`` capture (empty trades) and a
        genuine zero-trade combo both yield the first-class NULL record.
    row
        The finalist's persisted :class:`~brutex.engine.results_store.ResultRow`
        — the source of ``zero_trades`` (``total_trades == 0``) and of the
        ``recovery_factor`` inputs; MUST be the same finalist as *capture*.
    lens
        The tuple's :class:`~brutex.engine.session_lens.SessionLens`.
    composite_score
        The finalist's COMMON composite score (computed tuple-wide by the
        run-end pass via ``brutex.ranking``'s one composite expression), or
        ``None`` when not computed. Stored full precision, never re-derived
        here (weights live in ONE place).
    fingerprint
        The finalist's behavioral-twin fingerprint from
        ``brutex.engine.twin_detect.trade_fingerprint`` (WP4) — delegated by
        parameter (module docstring).
    start_equity_paisa
        The equity base the capture's session series was built with (MUST
        equal ``capture_combo``'s ``start_equity_paisa`` — it anchors the
        minute-path drawdown peak).

    Returns
    -------
    ComboAggregate
        The per-combo record, ready for ``to_record()`` →
        ``write_tuple_sidecars``.

    Raises
    ------
    ComboAggregateError
        If *capture* and *row* disagree on ``run_id`` (a mislabelled join
        would silently attribute one combo's numbers to another).
    """
    if capture.run_id != row.run_id:
        raise ComboAggregateError(
            f"aggregate_combo: capture run_id {capture.run_id!r} != row run_id {row.run_id!r}"
        )
    trades = capture.trades
    n_trades = len(trades)
    stats_live = capture.parity_ok and n_trades > 0

    mae_stats: tuple[int, int, int] | None = None
    mfe_stats: tuple[int, int, int] | None = None
    mae_1m_stats: tuple[int, int, int] | None = None
    mfe_1m_stats: tuple[int, int, int] | None = None
    trades_with_1m = 0
    max_dd_paisa: int | None = None
    max_dd_pct: float | None = None
    worst5_json = _dumps([])
    vix_win: tuple[float, float, float] | None = None
    vix_loss: tuple[float, float, float] | None = None
    band: tuple[float, float] | None = None
    vix_stamped = 0
    pnl_by_year: dict[str, int] = {}
    pnl_by_trend: dict[str, int] = {}
    wonder = False
    wonder_axis: str | None = None
    hour_hist: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    event_pnl: dict[str, int] = {}
    exit_tally: dict[str, int] = dict.fromkeys(EXIT_REASONS, 0)
    win_loss_ratio: float | None = None
    recovery_factor: float | None = None

    if stats_live:
        mae_stats = _int_stats([trade.mae_paisa for trade in trades])
        mfe_stats = _int_stats([trade.mfe_paisa for trade in trades])
        ok_trades = [t for t in trades if t.path_1m_status is PathStatus.OK]
        trades_with_1m = len(ok_trades)
        if trades_with_1m == n_trades:
            # PathStatus.OK guarantees the 1m pair is present (the
            # CapturedTrade invariant) — cast, no runtime re-check.
            mae_1m_stats = _int_stats([cast(int, t.mae_1m_paisa) for t in ok_trades])
            mfe_1m_stats = _int_stats([cast(int, t.mfe_1m_paisa) for t in ok_trades])
        max_dd_paisa, max_dd_pct = minute_path_max_drawdown(
            capture.equity, start_equity_paisa=start_equity_paisa
        )
        worst5_json = _worst_days_json(trades)

        stamped = [(float(t.vix_entry), t.pnl_paisa) for t in trades if t.vix_entry is not None]
        vix_stamped = len(stamped)
        win_vix = sorted(level for level, pnl in stamped if pnl > 0)
        loss_vix = sorted(level for level, pnl in stamped if pnl < 0)
        if win_vix:
            vix_win = _float_stats(win_vix)
            band = (
                cast(float, nearest_rank(win_vix, _PCT_P10)),
                cast(float, nearest_rank(win_vix, _PCT_P90)),
            )
        if loss_vix:
            vix_loss = _float_stats(loss_vix)

        net_pnl = 0
        for trade in trades:
            net_pnl += trade.pnl_paisa
            exit_tally[trade.exit_reason] += 1
            year_key = str(ist_trade_date(trade.exit_ts).year)
            pnl_by_year[year_key] = pnl_by_year.get(year_key, 0) + trade.pnl_paisa
            entry_date = ist_trade_date(trade.entry_ts)
            regime = lens.trend_by_date.get(entry_date)
            trend_key = TREND_UNKNOWN if regime is None else regime.value
            pnl_by_trend[trend_key] = pnl_by_trend.get(trend_key, 0) + trade.pnl_paisa
            hour_key = str(entry_hour_ist(trade.entry_ts))
            hour_hist[hour_key] = hour_hist.get(hour_key, 0) + 1
            for flag in lens.event_flags_by_date.get(entry_date, ()):
                event_counts[flag] = event_counts.get(flag, 0) + 1
                event_pnl[flag] = event_pnl.get(flag, 0) + trade.pnl_paisa

        if net_pnl > 0:
            known_trend_pnls = [
                value for key, value in pnl_by_trend.items() if key != TREND_UNKNOWN
            ]
            if _one_regime_dominates(pnl_by_year.values()):
                wonder = True
                wonder_axis = _AXIS_YEAR
            elif _one_regime_dominates(known_trend_pnls):
                wonder = True
                wonder_axis = _AXIS_TREND

        winners = sum(1 for t in trades if t.pnl_paisa > 0)
        losers = sum(1 for t in trades if t.pnl_paisa < 0)
        gross_profit = sum(t.pnl_paisa for t in trades if t.pnl_paisa > 0)
        gross_loss_abs = -sum(t.pnl_paisa for t in trades if t.pnl_paisa < 0)
        avg_win = gross_profit // winners if winners else 0
        avg_loss_abs = gross_loss_abs // losers if losers else 0
        if avg_loss_abs > 0:
            win_loss_ratio = float(avg_win) / float(avg_loss_abs)
        if row.max_drawdown_paisa != 0:
            recovery_factor = float(row.net_pnl_paisa) / float(row.max_drawdown_paisa)

    max_losses, max_wins = _consecutive_runs([t.pnl_paisa for t in trades])
    return ComboAggregate(
        run_id=capture.run_id,
        n_trades=n_trades,
        zero_trades=row.total_trades == 0,
        parity_ok=capture.parity_ok,
        mae_avg_paisa=None if mae_stats is None else mae_stats[0],
        mae_worst_paisa=None if mae_stats is None else mae_stats[1],
        mae_p95_paisa=None if mae_stats is None else mae_stats[2],
        mfe_avg_paisa=None if mfe_stats is None else mfe_stats[0],
        mfe_best_paisa=None if mfe_stats is None else mfe_stats[1],
        mfe_p95_paisa=None if mfe_stats is None else mfe_stats[2],
        mae_1m_avg_paisa=None if mae_1m_stats is None else mae_1m_stats[0],
        mae_1m_worst_paisa=None if mae_1m_stats is None else mae_1m_stats[1],
        mae_1m_p95_paisa=None if mae_1m_stats is None else mae_1m_stats[2],
        mfe_1m_avg_paisa=None if mfe_1m_stats is None else mfe_1m_stats[0],
        mfe_1m_best_paisa=None if mfe_1m_stats is None else mfe_1m_stats[1],
        mfe_1m_p95_paisa=None if mfe_1m_stats is None else mfe_1m_stats[2],
        trades_with_1m=trades_with_1m,
        max_dd_minute_path_paisa=max_dd_paisa,
        max_dd_minute_path_pct=max_dd_pct,
        equity_status=capture.equity_status.value,
        worst5_days_json=worst5_json,
        vix_win_min=None if vix_win is None else vix_win[0],
        vix_win_avg=None if vix_win is None else vix_win[1],
        vix_win_max=None if vix_win is None else vix_win[2],
        vix_loss_min=None if vix_loss is None else vix_loss[0],
        vix_loss_avg=None if vix_loss is None else vix_loss[1],
        vix_loss_max=None if vix_loss is None else vix_loss[2],
        vix_band_lo=None if band is None else band[0],
        vix_band_hi=None if band is None else band[1],
        vix_stamped_trades=vix_stamped,
        vix_missing_trades=capture.vix_missing_trades,
        pnl_by_year_json=_dumps(pnl_by_year),
        pnl_by_trend_json=_dumps(pnl_by_trend),
        one_regime_wonder=wonder,
        regime_wonder_axis=wonder_axis,
        entry_hour_hist_json=_dumps(hour_hist),
        event_day_counts_json=_dumps(event_counts),
        event_day_pnl_json=_dumps(event_pnl),
        exits_stop_loss=exit_tally["stop_loss"],
        exits_take_profit=exit_tally["take_profit"],
        exits_signal=exit_tally["signal"],
        exits_forced_1520=exit_tally["forced_1520"],
        exits_end_of_data=exit_tally["end_of_data"],
        max_consecutive_losses=max_losses,
        max_consecutive_wins=max_wins,
        composite_score=composite_score,
        win_loss_ratio=win_loss_ratio,
        recovery_factor=recovery_factor,
        fingerprint=fingerprint,
    )

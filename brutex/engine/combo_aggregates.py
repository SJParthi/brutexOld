"""Per-combo aggregate record for the results-depth pass (R22/R31/R46/R51).

DEC-SWEEP-RESULTS-DEPTH-001 (WP2). :func:`aggregate_combo` folds ONE finalist's
:class:`~brutex.engine.trade_capture.ComboCapture` + the tuple's
:class:`~brutex.engine.session_lens.SessionLens` into ONE
:class:`ComboAggregate` — the record persisted to ``<stem>.aggs.parquet``
(schema authority: :data:`brutex.engine.trade_store.AGG_COLUMNS`; a test pins
``ComboAggregate.to_record()`` keys against it so the two can never drift).

**RUST-HOSTED LAW (DEC-RUST-CASCADE-RESULTS-001):** the whole fold — the
nearest-rank / floor-mean / clamped-float statistics, the worst-5-days view,
the VIX stats + band, the pinned one-regime-wonder rule, the lens
attributions (IST dates/hours via the crate's fixed +05:30 law), the
exit-reason tallies, the consecutive-runs fold, the ``win_loss_ratio`` /
``recovery_factor`` laws and the CPython-``json.dumps``-byte-identical lens
renders — lives in ``rust/brutex-cascade/src/combo_aggregates.rs``, verified
against the FROZEN pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/combo_aggregates.json``). This
module is the type face: the :class:`ComboAggregate` frozen dataclass (whose
``__post_init__`` invariants re-validate every fold output), the domain
error, the run_id-mismatch refusal and the marshalling boundary.

## Field → computed-source traceability (R27 "no invented text anywhere")

The laws (each now Rust-hosted, semantics unchanged):

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from brutex.engine.cascade_rust import epoch_day, epoch_us, require_cascade_rust
from brutex.engine.minute_path import PathStatus
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

    from brutex.engine.results_store import ResultRow
    from brutex.engine.session_lens import SessionLens
    from brutex.engine.trade_capture import ComboCapture

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
#: ranking gate. (The load-bearing constant lives in the Rust law —
#: ``REGIME_WONDER_SHARE`` in ``combo_aggregates.rs``; parity-pinned.)
REGIME_WONDER_SHARE: Final[float] = 0.8

#: The worst-days stress view depth (R22 "explicit worst-5-days stress view").
WORST_DAYS_LIMIT: Final[int] = 5

#: The trend bucket for a trade whose entry date the lens cannot label (a
#: session outside the lens's daily-bar coverage) — an honest bucket, never a
#: silent drop and never a guessed regime.
TREND_UNKNOWN: Final[str] = "unknown"

#: Integer percent-rank domain bound for :func:`nearest_rank`.
_PCT_MAX: Final[int] = 100


class ComboAggregateError(BruteXError):
    """Raised on a broken aggregate invariant (mismatched join, bad tallies)."""


def nearest_rank(sorted_values: Sequence[int] | Sequence[float], pct: int) -> int | float:
    """Return the nearest-rank *pct*-th percentile of pre-sorted values.

    The design-pinned deterministic law: ``rank = ceil(pct · n / 100)`` (with
    EXACT integer arithmetic — ``-(-pct · n // 100)`` — so no float rounding
    can ever move a rank; the rank arithmetic is the Rust crate's
    ``nearest_rank_index``), value = ``sorted_values[rank - 1]``. No
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
    rs = require_cascade_rust()
    index = cast(int, rs.nearest_rank_index(n, pct))
    return sorted_values[index]


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
    is an ULP-artifact correction, never a rounding). Rust-hosted
    (``float_stats`` in ``combo_aggregates.rs``); this wrapper survives for
    its direct unit pins.
    """
    rs = require_cascade_rust()
    lo, mean, hi = rs.float_stats(list(values))
    return lo, mean, hi


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
      the crate's ``minute_path`` drawdown law over the capture's
      per-session equity series (peak-anchored at the sim's starting
      equity); ``pct`` full precision and tracked INDEPENDENTLY of the paisa
      maximum.
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
      ``metrics.compute._consecutive_runs`` law, hosted in the crate).
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
    exits_session_gap: int
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
            self.exits_session_gap,
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
            "exits_session_gap": self.exits_session_gap,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_consecutive_wins": self.max_consecutive_wins,
            "composite_score": self.composite_score,
            "win_loss_ratio": self.win_loss_ratio,
            "recovery_factor": self.recovery_factor,
            "fingerprint": self.fingerprint,
        }


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
    rs = require_cascade_rust()

    fold = rs.aggregate_fold(
        [epoch_us(t.entry_ts) for t in trades],
        [epoch_us(t.exit_ts) for t in trades],
        [t.pnl_paisa for t in trades],
        [t.mae_paisa for t in trades],
        [t.mfe_paisa for t in trades],
        [t.mae_1m_paisa for t in trades],
        [t.mfe_1m_paisa for t in trades],
        [t.path_1m_status is PathStatus.OK for t in trades],
        [t.exit_reason for t in trades],
        [None if t.vix_entry is None else float(t.vix_entry) for t in trades],
        [epoch_day(day) for day in lens.trend_by_date],
        [regime.value for regime in lens.trend_by_date.values()],
        [epoch_day(day) for day in lens.event_flags_by_date],
        [list(flags) for flags in lens.event_flags_by_date.values()],
        [point.equity_close_paisa for point in capture.equity],
        [point.equity_intraday_low_paisa for point in capture.equity],
        row.total_trades,
        row.net_pnl_paisa,
        row.max_drawdown_paisa,
        capture.parity_ok,
        start_equity_paisa,
    )
    return ComboAggregate(
        run_id=capture.run_id,
        n_trades=fold["n_trades"],
        zero_trades=fold["zero_trades"],
        parity_ok=capture.parity_ok,
        mae_avg_paisa=fold["mae_avg_paisa"],
        mae_worst_paisa=fold["mae_worst_paisa"],
        mae_p95_paisa=fold["mae_p95_paisa"],
        mfe_avg_paisa=fold["mfe_avg_paisa"],
        mfe_best_paisa=fold["mfe_best_paisa"],
        mfe_p95_paisa=fold["mfe_p95_paisa"],
        mae_1m_avg_paisa=fold["mae_1m_avg_paisa"],
        mae_1m_worst_paisa=fold["mae_1m_worst_paisa"],
        mae_1m_p95_paisa=fold["mae_1m_p95_paisa"],
        mfe_1m_avg_paisa=fold["mfe_1m_avg_paisa"],
        mfe_1m_best_paisa=fold["mfe_1m_best_paisa"],
        mfe_1m_p95_paisa=fold["mfe_1m_p95_paisa"],
        trades_with_1m=fold["trades_with_1m"],
        max_dd_minute_path_paisa=fold["max_dd_minute_path_paisa"],
        max_dd_minute_path_pct=fold["max_dd_minute_path_pct"],
        equity_status=capture.equity_status.value,
        worst5_days_json=fold["worst5_days_json"],
        vix_win_min=fold["vix_win_min"],
        vix_win_avg=fold["vix_win_avg"],
        vix_win_max=fold["vix_win_max"],
        vix_loss_min=fold["vix_loss_min"],
        vix_loss_avg=fold["vix_loss_avg"],
        vix_loss_max=fold["vix_loss_max"],
        vix_band_lo=fold["vix_band_lo"],
        vix_band_hi=fold["vix_band_hi"],
        vix_stamped_trades=fold["vix_stamped_trades"],
        vix_missing_trades=capture.vix_missing_trades,
        pnl_by_year_json=fold["pnl_by_year_json"],
        pnl_by_trend_json=fold["pnl_by_trend_json"],
        one_regime_wonder=fold["one_regime_wonder"],
        regime_wonder_axis=fold["regime_wonder_axis"],
        entry_hour_hist_json=fold["entry_hour_hist_json"],
        event_day_counts_json=fold["event_day_counts_json"],
        event_day_pnl_json=fold["event_day_pnl_json"],
        exits_stop_loss=fold["exits_stop_loss"],
        exits_take_profit=fold["exits_take_profit"],
        exits_signal=fold["exits_signal"],
        exits_forced_1520=fold["exits_forced_1520"],
        exits_end_of_data=fold["exits_end_of_data"],
        exits_session_gap=fold["exits_session_gap"],
        max_consecutive_losses=fold["max_consecutive_losses"],
        max_consecutive_wins=fold["max_consecutive_wins"],
        composite_score=composite_score,
        win_loss_ratio=fold["win_loss_ratio"],
        recovery_factor=fold["recovery_factor"],
        fingerprint=fingerprint,
    )

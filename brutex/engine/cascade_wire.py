"""Stage-4 cascade wiring — the COMMON adapter that runs the 9-filter cascade on
a sweep's persisted survivors, BOTH automatically (on every sweep) and manually
(``brutex cascade <run-id>``).

Closes ``docs/TRACK1_GAP_ANALYSIS.md`` §2 item #1 + §6 "Stage-4 cascade" row:
the nine overfitting filters (:func:`brutex.metrics.cascade.run_stage4_cascade`)
existed as a pure orchestrator but were never invoked by a real sweep — only the
single-filter ``--overfit-gate`` (Deflated-Sharpe) ran inline. This module is the
**series-regeneration adapter** the cascade docstring flagged as "out of scope
here (it needs real bars)": it rebuilds every per-candidate return series the nine
filters need **deterministically from the persisted ``mask_bits`` + the real
bars** — never from win/loss aggregates (that would FABRICATE significance and is
forbidden, Golden Rule #1; see :mod:`brutex.ranking.overfit`'s honest envelope).

COMMON — one cascade, two call sites (CLAUDE.md §11)
---------------------------------------------------
:func:`run_cascade_for_survivors` is the SINGLE seam. The automatic path
(:func:`brutex.engine.sweep_orchestrator.run_sweep_grid` → the CLI's auto-cascade
hook) and the manual path (``brutex cascade`` → :func:`cascade_report_for_store`)
both call it; neither re-implements any cascade logic. A golden byte-equality test
asserts the two paths produce identical :class:`~brutex.metrics.cascade.CascadeReport`\\s
for the same inputs — the key correctness guarantee.

Deterministic series reconstruction (Golden Rule #4)
----------------------------------------------------
:func:`build_candidate_input` takes one stored ``(mask_bits, ...)`` finalist plus
its bars and:

1. Rebuilds the Stage-1A ``bar_bits`` (``proximity`` + ``prior_n`` must match the
   sweep's — they are part of the run identity), once per ``(bars, proximity,
   prior_n)`` via the injected ``bar_bits`` (reused across a tuple's finalists).
2. Re-simulates the mask's trades (:func:`brutex.engine.trade_sim.simulate_trades`)
   with the SAME sl/tp/tf knobs the sweep used — the byte-identical trade stream
   ``ranked_combo_to_result_row`` already reconstructs (DEC-SWEEP-WINNERS-BY-NAME-001).
3. Derives the cascade's per-candidate inputs from those real trades + the real
   :class:`~brutex.metrics.compute.ResultMetrics` (recomputed via
   :func:`brutex.engine.sweep_driver.metrics_for_mask`, so the scalars are the
   exact sweep values, not the persisted subset): the per-trade ``returns`` series
   (the same ``pnl_paisa / start_equity`` convention as
   :func:`brutex.metrics.compute.compute_metrics`), the lockbox returns+days, and
   the Monte-Carlo ``evaluate`` closure (relocate entries → re-run the SAME exit
   logic → Sharpe).

Because every input is a pure function of ``(mask_bits, bars, knobs)``, the
candidate — and thus the cascade verdict — is byte-identical on a re-run, whether
reached automatically or manually (Golden Rule #4 / #7).

Honest envelope (Golden Rule #10) — NO O(1)-total claim
-------------------------------------------------------
This is offline Stage-4 batch work over the top ~K finalists, NOT an O(1) hot
path. Per candidate the setup is O(N_trades) (the re-simulation) plus the cascade
engine's own O(K) per-candidate filter calls and pool bootstraps; the Monte-Carlo
``evaluate`` closure is O(N_trades) per permutation × B permutations. The bars are
loaded ONCE per ``(instrument, tf)`` and the ``bar_bits`` built ONCE per
``(bars, proximity, prior_n)`` (reused across a tuple's finalists) — bounded
``O(N_bars)`` memory, never O(1) total. Claiming "O(1) total" for a cascade over a
whole sweep would be a physical impossibility and is FORBIDDEN.

Trade-return reconstruction caveat (honest): the cascade's pool filters
(SPA / Romano-Wolf / PBO) need every pooled candidate's returns on a **common
time axis**, but two masks fire different numbers of trades, so per-*trade*
returns have different lengths. :func:`build_candidate_input` therefore projects
each candidate's trades onto a **per-bar return series** of length ``len(bars)``
(a bar with no closing trade contributes ``0.0``) — a common axis shared by every
candidate over the same bars. The per-candidate filters (DSR / CPCV / WFO /
Lockbox / Monte-Carlo) use the natural per-trade series. Both are deterministic
functions of the real trades; neither fabricates a value (Golden Rule #1).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.models import CandleBar
from brutex.engine.cascade_sidecar import CascadeSidecarFlush, write_cascade_sidecar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import metrics_for_mask
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    CascadeReport,
    run_stage4_cascade,
)
from brutex.metrics.cpcv import sharpe_of_returns
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

    from brutex.engine.results_store import ResultRow, ResultsStore

_LOG = structlog.get_logger("brutex.engine.cascade_wire")

__all__ = [
    "DEFAULT_CASCADE_TOP_K",
    "CascadeAutoResult",
    "CascadeSurvivorSpec",
    "build_candidate_input",
    "cascade_report_for_store",
    "manual_cascade_report",
    "run_cascade_for_survivors",
    "run_cascade_on_store",
]

#: Default number of top-ranked finalists per tuple to feed the cascade. The
#: cascade is offline Stage-4 batch work over the funnel's final candidates
#: (``TRACK1_BACKTEST_SPEC.md`` §"Stage 4"); a small, bounded K keeps the pool
#: bootstraps tractable. Opt-in higher via the CLI ``--top-k``.
DEFAULT_CASCADE_TOP_K: Final[int] = 50


@dataclass(frozen=True, slots=True)
class CascadeSurvivorSpec:
    """One persisted finalist + the knobs needed to reconstruct its series.

    The cascade needs each candidate's real per-period return series, which the
    persisted :class:`~brutex.engine.results_store.ResultRow` does not carry — only
    scalar metrics. This spec pairs a finalist's identity (``run_id`` + ``mask_bits``
    + ``direction``) with the trade-sim knobs (``tf_minutes`` / ``sl_paisa`` /
    ``tp_paisa`` / ``proximity`` / ``prior_n`` / ``start_equity_paisa``) so
    :func:`build_candidate_input` can re-simulate the byte-identical trade stream
    the sweep produced (DEC-SWEEP-WINNERS-BY-NAME-001) and derive the series.

    Attributes
    ----------
    run_id
        The 64-char BLAKE3 run-identity — the cascade ``candidate_id`` (so the
        verdict maps straight back to the persisted row).
    mask_bits
        The u64 strategy mask (the entry rule to re-simulate).
    direction
        The trade direction (:data:`~brutex.strategies.types.Side.BUY` / ``SELL``).
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    sl_paisa, tp_paisa
        Stop-loss / take-profit distances in paisa (``> 0``), or ``None``.
    proximity
        Proximity tolerances for the ``near_*`` conditions, or ``None`` for the
        Stage-1A default — MUST match the sweep's (part of the run identity).
    prior_n
        Prior-bar look-back used by the sweep (``>= 1``) — MUST match.
    start_equity_paisa
        Notional capital base (paisa, ``> 0``) — the per-trade return denominator.

    Raises
    ------
    ValueError
        If ``run_id`` is empty, ``mask_bits`` is negative, ``direction`` is
        ``HOLD``, ``tf_minutes < 1``, a paisa leg is ``<= 0``, ``prior_n < 1``, or
        ``start_equity_paisa <= 0``.
    """

    run_id: str
    mask_bits: int
    direction: Side
    tf_minutes: int
    sl_paisa: int | None
    tp_paisa: int | None
    proximity: ProximityParams | None
    prior_n: int
    start_equity_paisa: int

    def __post_init__(self) -> None:
        """Validate the survivor spec fields (non-empty run_id, non-negative mask)."""
        if not self.run_id:
            raise ValueError("CascadeSurvivorSpec.run_id must be non-empty")
        if self.mask_bits < 0:
            raise ValueError(f"CascadeSurvivorSpec.mask_bits must be >= 0, got {self.mask_bits}")
        if self.direction is Side.HOLD:
            raise ValueError(
                "CascadeSurvivorSpec.direction must be Side.BUY or Side.SELL, not HOLD"
            )
        if self.tf_minutes < 1:
            raise ValueError(f"CascadeSurvivorSpec.tf_minutes must be >= 1, got {self.tf_minutes}")
        for name in ("sl_paisa", "tp_paisa"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"CascadeSurvivorSpec.{name} must be > 0 or None, got {value}")
        if self.prior_n < 1:
            raise ValueError(f"CascadeSurvivorSpec.prior_n must be >= 1, got {self.prior_n}")
        if self.start_equity_paisa <= 0:
            raise ValueError(
                f"CascadeSurvivorSpec.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )


def _per_bar_returns(
    bars: Sequence[CandleBar],
    trades: Sequence[Any],
    *,
    start_equity_paisa: int,
) -> tuple[float, ...]:
    """Project a candidate's trades onto a per-bar return series of length ``len(bars)``.

    The pool filters (SPA / Romano-Wolf / PBO) need every candidate's returns on a
    **common time axis**, but per-trade series have different lengths (different
    masks fire different trade counts). Each closed trade's return
    ``pnl_paisa / start_equity_paisa`` (the same convention as
    :func:`brutex.metrics.compute.compute_metrics`) is placed at the bar index of its
    **exit** timestamp; bars with no closing trade contribute ``0.0``. This is a
    deterministic function of the real trades (Golden Rule #1 — no fabrication) and
    is identical for every candidate's axis (the shared bars), so the pool matrix is
    rectangular.

    Parameters
    ----------
    bars
        The bars the sweep ran on (the common time axis).
    trades
        The candidate's closed trades (each carries ``exit_ts`` + ``pnl_paisa``).
    start_equity_paisa
        The per-trade return denominator (``> 0``).

    Returns
    -------
    tuple[float, ...]
        A length-``len(bars)`` per-bar return series.
    """
    index_of_ts = {bar.timestamp: i for i, bar in enumerate(bars)}
    series = [0.0] * len(bars)
    for trade in trades:
        idx = index_of_ts[trade.exit_ts]
        series[idx] += trade.pnl_paisa / start_equity_paisa
    return tuple(series)


def build_candidate_input(
    spec: CascadeSurvivorSpec,
    bars: Sequence[CandleBar],
    *,
    bar_bits: Sequence[int] | None = None,
) -> CandidateInput:
    """Reconstruct one cascade :class:`~brutex.metrics.cascade.CandidateInput` deterministically.

    Re-derives every input the nine filters need from the persisted ``mask_bits``
    + the real ``bars`` (see the module docstring): the scalar metrics (recomputed
    via :func:`brutex.engine.sweep_driver.metrics_for_mask`, so they are the exact
    sweep values), the per-bar ``returns`` series on the shared time axis, the
    lockbox returns+days, and the Monte-Carlo ``evaluate`` closure. Pure +
    deterministic — the same ``(spec, bars)`` always yields the same candidate
    (Golden Rule #4).

    A ``None`` scalar metric (mathematically undefined for the sample — e.g.
    ``sortino`` with no downside) maps to ``0.0`` (the same documented choice as
    :func:`brutex.engine.sweep_run.ranked_combo_to_result_row`); the
    :class:`CandidateInput` finiteness validation then never sees a ``None``.

    Parameters
    ----------
    spec
        The finalist's identity + trade-sim knobs.
    bars
        The bars the sweep ran on (non-empty; the same ``(instrument, interval)``
        slice).
    bar_bits
        Optional pre-built Stage-1A matrix for ``(bars, spec.proximity,
        spec.prior_n)`` — pass it to reuse across a tuple's finalists (built once).
        ``None`` builds it inline.

    Returns
    -------
    CandidateInput
        The fully-populated, validated cascade candidate.

    Raises
    ------
    ValueError
        If ``bars`` is empty.
    """
    if not bars:
        raise ValueError("build_candidate_input: bars must be non-empty")
    bits = (
        build_bar_bits(bars, proximity=spec.proximity, prior_n=spec.prior_n)
        if bar_bits is None
        else bar_bits
    )
    metrics = metrics_for_mask(
        bars,
        bits,
        spec.mask_bits,
        direction=spec.direction,
        tf_minutes=spec.tf_minutes,
        stop_loss_paisa=spec.sl_paisa,
        take_profit_paisa=spec.tp_paisa,
        start_equity_paisa=spec.start_equity_paisa,
    )
    entry_signals = mask_to_entry_signals(bits, spec.mask_bits)
    trades = simulate_trades(
        bars,
        entry_signals,
        direction=spec.direction,
        tf_minutes=spec.tf_minutes,
        stop_loss_paisa=spec.sl_paisa,
        take_profit_paisa=spec.tp_paisa,
    )
    returns = _per_bar_returns(bars, trades, start_equity_paisa=spec.start_equity_paisa)

    # Per-trade returns + their trading-day ordinals for the lockbox filter.
    per_trade_returns = tuple(t.pnl_paisa / spec.start_equity_paisa for t in trades)
    lockbox_days = tuple(t.exit_ts.toordinal() for t in trades)

    # Monte-Carlo: relocate the entries within [0, n_bars) and re-run the SAME exit
    # logic (the trade walk), returning the resulting Sharpe. Deterministic in its
    # entry positions (the seed lives in CascadeConfig); a flat/degenerate series
    # yields a None Sharpe → treated by the MC engine as "did not beat".
    n_bars = len(bars)

    def _evaluate(positions: npt.NDArray[np.intp]) -> float:
        """Compute the held-out objective for one CPCV fold's test positions."""
        relocated = [False] * n_bars
        for pos in positions:
            relocated[int(pos)] = True
        perm_trades = simulate_trades(
            bars,
            relocated,
            direction=spec.direction,
            tf_minutes=spec.tf_minutes,
            stop_loss_paisa=spec.sl_paisa,
            take_profit_paisa=spec.tp_paisa,
        )
        perm_returns = [t.pnl_paisa / spec.start_equity_paisa for t in perm_trades]
        sharpe = sharpe_of_returns(perm_returns)
        return float("nan") if sharpe is None else sharpe

    mc_n_entries = max(1, sum(1 for s in entry_signals if s))

    return CandidateInput(
        candidate_id=spec.run_id,
        sharpe_ratio=_metric_or_zero(metrics.sharpe_ratio),
        sortino_ratio=_metric_or_zero(metrics.sortino_ratio),
        calmar_ratio=_metric_or_zero(metrics.calmar_ratio),
        profit_factor=_metric_or_zero(metrics.profit_factor),
        win_rate=metrics.win_rate,
        win_loss_ratio=_metric_or_zero(metrics.win_loss_ratio),
        recovery_factor=_metric_or_zero(metrics.recovery_factor),
        max_drawdown_pct=metrics.max_drawdown_pct,
        max_consecutive_losses=metrics.max_consecutive_losses,
        total_trades=metrics.total_trades,
        training_sharpe=_metric_or_zero(metrics.sharpe_ratio),
        training_max_drawdown=metrics.max_drawdown_pct,
        skew=_metric_or_zero(metrics.skewness),
        kurtosis=_metric_or_zero(metrics.kurtosis),
        returns=returns,
        lockbox_returns=per_trade_returns,
        lockbox_days=lockbox_days,
        mc_n_bars=n_bars,
        mc_n_entries=mc_n_entries,
        mc_evaluate=_evaluate,
    )


def _metric_or_zero(value: float | None) -> float:
    """Map a ``float | None`` metric to a non-optional float (``None`` → ``0.0``).

    Mirrors :func:`brutex.engine.sweep_run._metric_or_zero`: ``ResultMetrics``
    ratios are ``None`` when mathematically undefined for the sample, but the
    cascade :class:`~brutex.metrics.cascade.CandidateInput` requires finite floats;
    a ``None`` becomes ``0.0`` (documented). Real values pass through unchanged.
    """
    return value if value is not None else 0.0


def run_cascade_for_survivors(
    specs: Sequence[CascadeSurvivorSpec],
    bars_for: Callable[[CascadeSurvivorSpec], Sequence[CandleBar]],
    config: CascadeConfig,
) -> CascadeReport:
    """Run the Stage-4 9-filter cascade over reconstructed survivors — the COMMON seam.

    The SINGLE function both the automatic (on-sweep) and manual (``brutex
    cascade``) paths call (CLAUDE.md §11 COMMON): it builds each candidate's
    :class:`~brutex.metrics.cascade.CandidateInput` via
    :func:`build_candidate_input` (reusing a per-(bars, proximity, prior_n)
    ``bar_bits`` across finalists that share an axis), then delegates to the
    golden-locked :func:`brutex.metrics.cascade.run_stage4_cascade`. No cascade
    logic is re-implemented here.

    Because the candidate reconstruction + the cascade are both pure functions of
    ``(specs, bars, config)``, two callers feeding the same inputs get a
    byte-identical :class:`~brutex.metrics.cascade.CascadeReport` (the golden
    auto-==-manual guarantee).

    Parameters
    ----------
    specs
        The persisted finalists to validate (each with its trade-sim knobs). Must
        have unique ``run_id``\\s (the cascade rejects duplicate ``candidate_id``\\s).
    bars_for
        ``spec -> bars`` loader (the bars the sweep ran that finalist on). Called
        once per spec; bars for finalists sharing a ``(bars-identity, proximity,
        prior_n)`` are reused for the ``bar_bits`` build via the per-axis cache.
    config
        The :class:`~brutex.metrics.cascade.CascadeConfig` (thresholds + seed).

    Returns
    -------
    CascadeReport
        The cascade's per-candidate verdicts + ranked survivor ids.

    Raises
    ------
    ValueError
        Propagated from :func:`build_candidate_input` (empty bars) or
        :func:`brutex.metrics.cascade.run_stage4_cascade` (duplicate ids / ragged
        pool axis).
    """
    # Per-axis bar_bits cache: finalists sharing the same bars object + proximity +
    # prior_n reuse one Stage-1A build (bounded by the number of distinct axes —
    # honest envelope: O(distinct-axes · N_bars) memory, never O(1) total).
    bits_cache: dict[tuple[int, int | None, int | None, int | None, int], Sequence[int]] = {}
    candidates: list[CandidateInput] = []
    for spec in specs:
        bars = bars_for(spec)
        key = (
            id(bars),
            None if spec.proximity is None else spec.proximity.pivot_proximity_bps,
            None if spec.proximity is None else spec.proximity.pdh_pdl_proximity_bps,
            None if spec.proximity is None else spec.proximity.fib_proximity_bps,
            spec.prior_n,
        )
        bits = bits_cache.get(key)
        if bits is None:
            bits = build_bar_bits(bars, proximity=spec.proximity, prior_n=spec.prior_n)
            bits_cache[key] = bits
        candidates.append(build_candidate_input(spec, bars, bar_bits=bits))
    report = run_stage4_cascade(candidates, config)
    _LOG.info(
        "cascade_wire.complete",
        n_input=report.n_input,
        n_survived=report.n_survived,
        seed=config.seed,
    )
    return report


@dataclass(frozen=True, slots=True)
class CascadeAutoResult:
    """The outcome of an automatic on-sweep cascade over one tuple's store.

    Attributes
    ----------
    report
        The :class:`~brutex.metrics.cascade.CascadeReport` (verdicts + survivors).
    sidecar
        The :class:`~brutex.engine.cascade_sidecar.CascadeSidecarFlush` recording
        where the verdict Parquet was written + its content hash, or ``None`` when
        the store held no rows (nothing to cascade — no sidecar written).
    """

    report: CascadeReport | None
    sidecar: CascadeSidecarFlush | None


def run_cascade_on_store(
    store: ResultsStore,
    bars: Sequence[CandleBar],
    knobs: dict[str, Any],
    config: CascadeConfig,
    *,
    sidecar_path: Path,
    top_k: int = DEFAULT_CASCADE_TOP_K,
    sort_by: str = "sharpe_ratio",
) -> CascadeAutoResult:
    """Run the cascade on a per-tuple store's top-K and persist the verdict sidecar.

    The AUTOMATIC on-sweep entry point: after a tuple's
    :func:`~brutex.engine.sweep_run.run_persisted_sweep` flushes its ranked
    finalists, this takes the store's top-``top_k`` rows (by ``sort_by``), runs the
    SAME :func:`run_cascade_for_survivors` the manual path uses (CLAUDE.md §11
    COMMON), and writes the report to *sidecar_path* via
    :func:`brutex.engine.cascade_sidecar.write_cascade_sidecar`. All rows share one
    ``(instrument, tf)`` bar window, so a single ``bars`` is passed for every
    finalist and the ``bar_bits`` build is reused.

    An empty store is a no-op (no rows → no cascade → no sidecar): the returned
    :class:`CascadeAutoResult` has both fields ``None`` so a caller can tell "no
    finalists" from "cascade ran".

    Parameters
    ----------
    store
        The flushed per-tuple :class:`~brutex.engine.results_store.ResultsStore`.
    bars
        The bars the tuple's sweep ran on (the shared time axis for every row).
    knobs
        The trade-sim knobs (``tf_minutes`` / ``sl_paisa`` / ``tp_paisa`` /
        ``proximity`` / ``prior_n`` / ``start_equity_paisa``) the tuple was swept
        with — needed to re-simulate each finalist's series.
    config
        The :class:`~brutex.metrics.cascade.CascadeConfig`.
    sidecar_path
        Destination Parquet for the verdict sidecar.
    top_k
        How many top-ranked finalists to feed the cascade (``>= 1``).
    sort_by
        The store column to rank the top-K by (default ``"sharpe_ratio"``).

    Returns
    -------
    CascadeAutoResult
        The cascade report + the sidecar flush (both ``None`` for an empty store).

    Raises
    ------
    ValueError
        If ``top_k < 1`` (delegated to :meth:`ResultsStore.top_k`).
    """
    if len(store) == 0:
        return CascadeAutoResult(report=None, sidecar=None)
    rows = store.top_k(top_k, by=sort_by)
    report = cascade_report_for_store(
        rows,
        lambda _row: bars,
        lambda _row: knobs,
        config,
    )
    flush = write_cascade_sidecar(report, sidecar_path)
    return CascadeAutoResult(report=report, sidecar=flush)


def manual_cascade_report(
    rows: Sequence[ResultRow],
    bars_by_tf: Callable[[str, str], Sequence[CandleBar]],
    config: CascadeConfig,
    *,
    start_equity_paisa: int,
    top_k: int = DEFAULT_CASCADE_TOP_K,
) -> CascadeReport:
    """Run the cascade over already-loaded persisted *rows* — the MANUAL-path core.

    The 100%-testable body behind ``brutex cascade <results-root>``: it takes the
    finalists merged from a persisted results tree, takes the top-``top_k`` ranked
    EXACTLY as :meth:`brutex.engine.results_store.ResultsStore.top_k` ranks them (by
    ``sharpe_ratio`` descending, ``run_id``-ascending tie-break) — so it feeds the
    cascade the SAME ordered candidates the automatic on-sweep path does — loads
    each row's bars via *bars_by_tf* ``(instrument, tf) -> bars``, and runs the SAME
    :func:`cascade_report_for_store` the automatic path uses (CLAUDE.md §11 COMMON).

    Honest limitation (Golden Rule #10): the persisted
    :class:`~brutex.engine.results_store.ResultRow` schema does **not** carry the
    trade-sim knobs (``sl_paisa`` / ``tp_paisa`` / ``proximity`` / ``prior_n``), so
    the manual path reconstructs the series with the **engine defaults**
    (``sl=tp=None``, ``proximity=None`` ⇒ Stage-1A default 10 bps, ``prior_n=3``) and
    the supplied ``start_equity_paisa``. A sweep run with the SAME defaults (the
    ``sweep-all`` defaults) therefore reproduces the automatic verdict byte-for-byte;
    a sweep run with non-default SL/TP needs those knobs threaded through (a tracked
    follow-up — the row schema would have to persist them). This is stated, never
    silently wrong.

    Parameters
    ----------
    rows
        The persisted finalists (e.g. from
        :func:`brutex.engine.results_report.load_results_tree`).
    bars_by_tf
        ``(instrument, tf) -> bars`` loader.
    config
        The :class:`~brutex.metrics.cascade.CascadeConfig`.
    start_equity_paisa
        The per-trade return denominator (``> 0``).
    top_k
        How many top-ranked finalists (by ``sharpe_ratio``) to feed the cascade
        (``>= 1``).

    Returns
    -------
    CascadeReport
        The cascade verdicts.

    Raises
    ------
    ValueError
        If ``rows`` is empty, ``top_k < 1``, or ``start_equity_paisa <= 0``.
    """
    if not rows:
        raise ValueError("manual_cascade_report: no persisted rows to cascade")
    if top_k < 1:
        raise ValueError(f"manual_cascade_report: top_k must be >= 1, got {top_k}")
    if start_equity_paisa <= 0:
        raise ValueError(
            f"manual_cascade_report: start_equity_paisa must be > 0, got {start_equity_paisa}"
        )
    # Rank EXACTLY as ResultsStore.top_k(by="sharpe_ratio") does — by Sharpe
    # descending with a run_id-ascending tie-break (stable two-pass) — so the manual
    # path feeds the cascade the SAME ordered candidates the automatic on-sweep path
    # does, and the two reports are byte-identical (the golden auto-==-manual
    # guarantee; CLAUDE.md §11 COMMON).
    ordered = sorted(rows, key=lambda r: r.run_id)
    ordered.sort(key=lambda r: r.sharpe_ratio, reverse=True)
    ranked = ordered[:top_k]
    knobs = {
        "tf_minutes": None,  # filled per-row below from the row's own tf label
        "sl_paisa": None,
        "tp_paisa": None,
        "proximity": None,
        "prior_n": 3,
        "start_equity_paisa": start_equity_paisa,
    }

    def _knobs_for(row: ResultRow) -> dict[str, Any]:
        """Return the per-row reconstruction knobs with the row's tf resolved to minutes."""
        return {**knobs, "tf_minutes": _tf_minutes(row.tf)}

    return cascade_report_for_store(
        ranked,
        lambda row: bars_by_tf(row.instrument, row.tf),
        _knobs_for,
        config,
    )


def _tf_minutes(tf_label: str) -> int:
    """Resolve a lake interval label (e.g. ``"5minute"``) to whole minutes (``>= 1``).

    Uses the single-source-of-truth interval table
    (:data:`brutex.feed.resampler.INTERVAL_SECONDS`); an unknown label falls back to
    ``1`` minute (the 1m base) so the cascade re-simulation never crashes on a label
    the resampler does not know (the trade-sim only uses ``tf_minutes`` for the
    forced-exit bar detection — a defensive default, documented).
    """
    from brutex.feed.resampler import INTERVAL_SECONDS

    seconds = INTERVAL_SECONDS.get(tf_label)
    if seconds is None:
        return 1
    return max(1, seconds // 60)


def cascade_report_for_store(
    rows: Sequence[ResultRow],
    bars_for: Callable[[ResultRow], Sequence[CandleBar]],
    knobs_for: Callable[[ResultRow], dict[str, Any]],
    config: CascadeConfig,
) -> CascadeReport:
    """Run the cascade over a persisted store's rows — the manual-path entry point.

    The seam ``brutex cascade <run-id>`` (and the dashboard route) call: map each
    :class:`~brutex.engine.results_store.ResultRow` to a
    :class:`CascadeSurvivorSpec` (its ``run_id`` + ``mask_bits`` + ``direction``,
    plus the trade-sim knobs the row's tuple was swept with — supplied by
    ``knobs_for`` since the row schema does not persist sl/tp/proximity/prior_n),
    then delegate to the COMMON :func:`run_cascade_for_survivors`. So the manual
    path runs the IDENTICAL cascade the automatic path does (CLAUDE.md §11).

    Parameters
    ----------
    rows
        The persisted finalists to validate (e.g. a store's top-K).
    bars_for
        ``row -> bars`` loader.
    knobs_for
        ``row -> {tf_minutes, sl_paisa, tp_paisa, proximity, prior_n,
        start_equity_paisa}`` — the trade-sim knobs not carried on the row schema.
    config
        The :class:`~brutex.metrics.cascade.CascadeConfig`.

    Returns
    -------
    CascadeReport
        The cascade verdicts.
    """
    specs: list[CascadeSurvivorSpec] = []
    bars_by_run_id: dict[str, Sequence[CandleBar]] = {}
    for row in rows:
        knobs = knobs_for(row)
        specs.append(
            CascadeSurvivorSpec(
                run_id=row.run_id,
                mask_bits=row.mask_bits,
                direction=Side.BUY if row.direction == "bull" else Side.SELL,
                tf_minutes=int(knobs["tf_minutes"]),
                sl_paisa=knobs["sl_paisa"],
                tp_paisa=knobs["tp_paisa"],
                proximity=knobs["proximity"],
                prior_n=int(knobs["prior_n"]),
                start_equity_paisa=int(knobs["start_equity_paisa"]),
            )
        )
        bars_by_run_id[row.run_id] = bars_for(row)

    return run_cascade_for_survivors(
        specs,
        lambda spec: bars_by_run_id[spec.run_id],
        config,
    )

"""Sweep report — one call from bars to the human-readable ranked top-combo table.

This is the reusable heart of the ``brutex sweep`` command (and any notebook /
dashboard view): given a window of bars and a chosen condition subset, it runs the
whole assembled funnel and renders the result as text a human can read directly —

    rank_sweep_over_bits  (sweep + Stage-1B prune + gate + composite rank)
      → explain_combos     (decode each winning mask into its condition NAMES)
      → render_leaderboard (the readable table)

plus a header stating exactly what was swept (instrument / TF / direction / the
combo space / how many cleared the gates) so the table is self-describing and
honest — no hidden assumptions (CLAUDE.md §1).

Pure — bars in, ``str`` out, no I/O. The CLI shell (``brutex sweep``) is the only
thing that touches the lake; it loads the bars then calls this. Money in the table
is rendered via :mod:`brutex.money`; statistical metrics are never rounded (only
*displayed* to a fixed width by the renderer — the stored values are untouched,
DEC-MONEY-2DP-001).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import count_combos
from brutex.engine.leaderboard import explain_combos, render_leaderboard
from brutex.engine.sweep_driver import (
    DEFAULT_MAX_COMBOS,
    DEFAULT_START_EQUITY_PAISA,
    rank_sweep_over_bits,
)
from brutex.engine.validation import OOSValidation, validate_out_of_sample
from brutex.ranking import MIN_TRADES, RankedCombo
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.exec_grid import ExecGridMap
    from brutex.engine.reference_series import TradeReferenceStamper

#: The leaderboard sort keys ``brutex sweep --sort-by`` accepts. ``composite`` is the
#: anti-overfit default ranking; ``pnl`` / ``win`` / ``pf`` answer "show me the
#: max-profit / max-win-rate / best-profit-factor combo" directly — opt-in, because
#: ordering on a single raw metric invites overfitting to one lucky combo (Golden
#: Rule #10), which the composite score deliberately resists.
SORT_KEYS: Final[tuple[str, ...]] = ("composite", "pnl", "win", "pf")


def sort_ranked(ranked: Sequence[RankedCombo], by: str) -> list[RankedCombo]:
    """Re-order the gate-passing leaderboard by a chosen headline metric (descending).

    ``composite`` keeps the anti-overfit composite-score order (the default); ``pnl``
    / ``win`` / ``pf`` order by raw net P&L / win-rate / profit-factor so the operator
    reads the max-profit or max-win-rate combo off the top. The sort is STABLE, so
    ties keep the incoming (composite) order — deterministic. A combo with an
    undefined ``profit_factor`` (no losing trades) sorts LAST under ``pf``.

    Raises
    ------
    ValueError
        If *by* is not one of :data:`SORT_KEYS`.
    """
    if by == "composite":
        return sorted(ranked, key=lambda rc: rc.composite_score, reverse=True)
    if by == "pnl":
        return sorted(ranked, key=lambda rc: rc.metrics.net_pnl_paisa, reverse=True)
    if by == "win":
        return sorted(ranked, key=lambda rc: rc.metrics.win_rate, reverse=True)
    if by == "pf":
        return sorted(
            ranked,
            key=lambda rc: (
                rc.metrics.profit_factor if rc.metrics.profit_factor is not None else float("-inf")
            ),
            reverse=True,
        )
    raise ValueError(f"sort_ranked: unknown sort key {by!r}; expected one of {SORT_KEYS}")


def _tick(held: bool) -> str:
    """``✓`` when a holdout component held, ``✗`` when it broke."""
    return "✓" if held else "✗"


def render_oos_verdict(oos: OOSValidation) -> str:
    """One-line out-of-sample verdict — did the combo's edge survive unseen data?

    Shows the overall ``HELD`` / ``FAILED`` verdict plus the three attributable
    components (gates / Sharpe / drawdown) so a failure is explainable: did the edge
    lose the hard gates on the test period, degrade on Sharpe, or blow drawdown?
    Includes the train/test day split + the test-period trade count for context.
    """
    verdict = "HELD ✓" if oos.held_up else "FAILED ✗"
    return (
        f"{verdict}  gates={_tick(oos.gates_held)} "
        f"sharpe={_tick(oos.sharpe_held)} dd={_tick(oos.drawdown_held)}  "
        f"(train {oos.train_days}d → test {oos.test_days}d, "
        f"test trades={oos.test_metrics.total_trades})"
    )


def oos_section(
    bars: Sequence[CandleBar],
    ranked: Sequence[RankedCombo],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
    proximity: ProximityParams | None,
    prior_n: int,
    train_frac: float = 0.7,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> str:
    """Render the out-of-sample (train → unseen) verdict for each ranked combo.

    Re-runs every combo on a train period vs the unseen test period that follows
    (:func:`~brutex.engine.validation.validate_out_of_sample`), so the operator sees
    whether a top combo's edge is REAL or just in-sample overfitting — the
    no-illusion check (CLAUDE.md §1). Needs ``>= 2`` distinct IST trading days; a
    shorter window renders an honest ``n/a`` (identical for every combo, so it is
    reported once). An empty board renders a clear "nothing to validate" line.
    """
    if not ranked:
        return "Out-of-sample: (no gate-passing combos to validate)"
    lines: list[str] = []
    for rank, rc in enumerate(ranked, start=1):
        try:
            oos = validate_out_of_sample(
                bars,
                rc.mask,
                direction=direction,
                tf_minutes=tf_minutes,
                exec_bars_1m=exec_bars_1m,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                start_equity_paisa=start_equity_paisa,
                train_frac=train_frac,
                proximity=proximity,
                prior_n=prior_n,
                roll_schedule=roll_schedule,
                apply_1day_mask=apply_1day_mask,
                reference_stamper=reference_stamper,
            )
        except ValueError as exc:
            # Too short to split (< 2 trading days) — the same for every combo.
            return f"Out-of-sample: n/a — {exc}"
        lines.append(f"  #{rank}  {render_oos_verdict(oos)}")
    head = f"Out-of-sample (train {train_frac:.0%} → unseen {1.0 - train_frac:.0%}):"
    return head + "\n" + "\n".join(lines)


def filter_oos_held(
    bars: Sequence[CandleBar],
    ranked: Sequence[RankedCombo],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
    proximity: ProximityParams | None,
    prior_n: int,
    train_frac: float = 0.7,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> list[RankedCombo]:
    """Keep only combos whose edge HELD out-of-sample — the no-illusion filter.

    Re-runs each combo through :func:`~brutex.engine.validation.validate_out_of_sample`
    and drops every one whose ``held_up`` is ``False`` (an in-sample winner that fell
    apart on the unseen test days). Order-preserving. Needs ``>= 2`` distinct IST
    trading days — a shorter window cannot be split, so the underlying
    ``ValueError`` propagates (the caller surfaces it as a clear user error).
    """
    return [
        rc
        for rc in ranked
        if validate_out_of_sample(
            bars,
            rc.mask,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            train_frac=train_frac,
            proximity=proximity,
            prior_n=prior_n,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            reference_stamper=reference_stamper,
        ).held_up
    ]


def sweep_report(
    bars: Sequence[CandleBar],
    *,
    instrument: str,
    tf_label: str,
    tf_minutes: int,
    direction: Side,
    condition_bits: Sequence[int],
    k: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    top_n: int | None = 20,
    min_hits: int = MIN_TRADES,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    max_combos: int = DEFAULT_MAX_COMBOS,
    sort_by: str = "composite",
    validate_oos: bool = False,
    oos_only: bool = False,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> str:
    """Run the funnel over *bars* and render the ranked top-combo table as text.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` (non-empty).
    instrument, tf_label
        Display labels for the header (e.g. ``"NIFTY"``, ``"5m"``). ``tf_label`` is
        cosmetic; the engine uses ``tf_minutes``.
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``) — the value the sweep uses.
    direction
        :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL`` (short).
    condition_bits, k
        The condition bit positions to brute-force over and how many each combo
        requires — passed straight to
        :func:`~brutex.engine.sweep_driver.rank_sweep_over_bits`.
    exec_bars_1m, exec_map
        The 1-minute EXECUTION stream (+ optional precomputed
        :class:`~brutex.engine.exec_grid.ExecGridMap`) every fill resolves on
        (DEC-FILL-EXEC-GRID-1M-001; REQUIRED — no silent single-feed shape,
        design §3.3). At ``tf_minutes == 1`` pass the same sequence as
        *bars*; a ``None`` map is derived once per sweep/OOS run.
    stop_loss_paisa, take_profit_paisa, top_n, min_hits, start_equity_paisa, proximity, prior_n, max_combos
        Sweep / render parameters; see
        :func:`~brutex.engine.sweep_driver.rank_sweep_over_bits` and
        :func:`~brutex.engine.leaderboard.render_leaderboard`. ``top_n`` limits the
        printed rows (default 20).
    sort_by
        Leaderboard ordering — one of :data:`SORT_KEYS` (default ``"composite"``,
        the anti-overfit ranking; ``"pnl"`` / ``"win"`` / ``"pf"`` order by the raw
        headline metric so the max-profit / max-win-rate combo is on top). See
        :func:`sort_ranked`.
    validate_oos
        When ``True``, append an out-of-sample section (:func:`oos_section`) that
        re-runs each shown combo on a train period vs the unseen test period that
        follows — the no-illusion "is this edge real or overfit?" check. Default
        ``False`` (off; it re-simulates each shown combo, and needs ``>= 2`` IST
        trading days).
    oos_only
        When ``True``, FILTER the board to combos whose edge held out-of-sample
        (:func:`filter_oos_held`) before rendering — so only the genuinely-robust
        combos are shown. The header reports ``oos-only=<held>/<gate survivors>``.
        Needs ``>= 2`` IST trading days (else a clear ``ValueError``).
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001):
        ``cmd_sweep`` threads ``resolution.roll_schedule`` +
        :func:`~brutex.engine.stage1a.apply_1day_mask_for` for the
        NIFTY-futures series alias; defaults are the byte-identical spot path.
    reference_stamper
        Optional India-VIX stamper (DEC-VIX-STAMP-WIRE-001) threaded to every
        trade sim behind the sweep + the OOS re-simulations. REFERENCE ONLY —
        the rendered report is byte-identical with or without it.

    Returns
    -------
    str
        A header (what was swept + how many combos cleared the gates) followed by
        the readable ranked leaderboard (or the honest "(no combos cleared the
        gates)" line).

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``k`` is outside ``[0, n_unique]``, or the combo
        count exceeds ``max_combos`` (the honest oversized-sweep guardrail).
    """
    if not bars:
        raise ValueError("sweep_report: bars must be non-empty")
    n_conditions = len(set(condition_bits))
    n_combos = count_combos(k, n_conditions)  # validates 0 <= k <= n_conditions
    ranked = rank_sweep_over_bits(
        bars,
        condition_bits,
        k,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        proximity=proximity,
        prior_n=prior_n,
        max_combos=max_combos,
        min_hits=min_hits,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
        reference_stamper=reference_stamper,
    )
    ranked = sort_ranked(ranked, sort_by)
    n_gate = len(ranked)
    oos_only_note = ""
    if oos_only:
        ranked = filter_oos_held(
            bars,
            ranked,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            reference_stamper=reference_stamper,
        )
        oos_only_note = f"  oos-only={len(ranked)}/{n_gate} held"
    table = render_leaderboard(explain_combos(ranked), top_n=top_n)
    header = (
        f"BruteX sweep — {instrument} {tf_label} {direction.value}\n"
        f"  conditions={n_conditions}  K={k}  combos={n_combos}  min_hits={min_hits}\n"
        f"  bars={len(bars)}  window={bars[0].timestamp.isoformat()}"
        f" → {bars[-1].timestamp.isoformat()}\n"
        f"  gate survivors={n_gate}  sort={sort_by}{oos_only_note}"
    )
    report = f"{header}\n\n{table}"
    if validate_oos:
        oos_ranked = ranked if top_n is None else ranked[:top_n]
        section = oos_section(
            bars,
            oos_ranked,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            reference_stamper=reference_stamper,
        )
        report = f"{report}\n\n{section}"
    return report


__all__ = [
    "SORT_KEYS",
    "filter_oos_held",
    "oos_section",
    "render_oos_verdict",
    "sort_ranked",
    "sweep_report",
]

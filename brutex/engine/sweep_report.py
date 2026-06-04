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
from typing import Final

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import count_combos
from brutex.engine.leaderboard import explain_combos, render_leaderboard
from brutex.engine.sweep_driver import (
    DEFAULT_MAX_COMBOS,
    DEFAULT_START_EQUITY_PAISA,
    rank_sweep_over_bits,
)
from brutex.ranking import MIN_TRADES, RankedCombo
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

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


def sweep_report(
    bars: Sequence[CandleBar],
    *,
    instrument: str,
    tf_label: str,
    tf_minutes: int,
    direction: Side,
    condition_bits: Sequence[int],
    k: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    top_n: int | None = 20,
    min_hits: int = MIN_TRADES,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    max_combos: int = DEFAULT_MAX_COMBOS,
    sort_by: str = "composite",
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
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        proximity=proximity,
        prior_n=prior_n,
        max_combos=max_combos,
        min_hits=min_hits,
    )
    ranked = sort_ranked(ranked, sort_by)
    table = render_leaderboard(explain_combos(ranked), top_n=top_n)
    header = (
        f"BruteX sweep — {instrument} {tf_label} {direction.value}\n"
        f"  conditions={n_conditions}  K={k}  combos={n_combos}  min_hits={min_hits}\n"
        f"  bars={len(bars)}  window={bars[0].timestamp.isoformat()}"
        f" → {bars[-1].timestamp.isoformat()}\n"
        f"  gate survivors={len(ranked)}  sort={sort_by}"
    )
    return f"{header}\n\n{table}"


__all__ = ["SORT_KEYS", "sort_ranked", "sweep_report"]

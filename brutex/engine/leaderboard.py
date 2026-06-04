"""Self-explaining leaderboard — pair each ranked combo with its matched conditions.

The sweep produces ranked combos (``brutex.ranking.RankedCombo``: a u64 mask + a
composite score). On its own a mask is an opaque number. This bridges the ranking
to the vocabulary decoder (``brutex.vocabulary.describe_mask``) so every leaderboard
row carries the EXACT conditions that fired, grouped by indicator family — and
renders them as readable text a CLI or dashboard can print directly.

Pure + O(1) per combo (a bounded decode of the 52-bit mask); no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from brutex.ranking import RankedCombo
from brutex.vocabulary.describe import MaskGroup, describe_mask


@dataclass(frozen=True, slots=True)
class ExplainedCombo:
    """A ranked combo paired with its decoded matched conditions.

    Attributes
    ----------
    ranked
        The :class:`~brutex.ranking.RankedCombo` (mask, metrics, composite score).
    groups
        The matched conditions grouped by indicator family (empty when the mask
        has no condition bits set, i.e. an "enter every bar" combo).
    """

    ranked: RankedCombo
    groups: tuple[MaskGroup, ...]


def explain_combos(ranked: Sequence[RankedCombo]) -> list[ExplainedCombo]:
    """Decode every ranked combo's mask into its matched conditions.

    Parameters
    ----------
    ranked
        The ranked leaderboard (already gate-passed + sorted by score).

    Returns
    -------
    list[ExplainedCombo]
        One per input combo, in the same order, each carrying its decoded
        condition groups.
    """
    return [ExplainedCombo(rc, tuple(describe_mask(rc.mask))) for rc in ranked]


def render_leaderboard(explained: Sequence[ExplainedCombo], *, top_n: int | None = None) -> str:
    """Render the (top-N) explained leaderboard as human-readable text.

    Each row shows the rank, composite score, risk-efficiency (MFE/MAE), trade
    count, the mask, and the matched conditions grouped by family — so the winning
    combo reads as a rule, not a number.

    Parameters
    ----------
    explained
        The explained leaderboard (see :func:`explain_combos`).
    top_n
        If given, render only the first ``top_n`` rows; otherwise render all.

    Returns
    -------
    str
        The rendered text (a single placeholder line when the leaderboard is
        empty — e.g. no combo cleared the gates).
    """
    rows = explained if top_n is None else explained[:top_n]
    if not rows:
        return "(no combos cleared the gates)"
    lines: list[str] = []
    for rank, ec in enumerate(rows, start=1):
        rc = ec.ranked
        conditions = " · ".join(f"{g.group}: {', '.join(g.conditions)}" for g in ec.groups)
        if not conditions:
            conditions = "(no conditions — enters every bar)"
        lines.append(
            f"#{rank}  score={rc.composite_score:+.3f}  "
            f"risk_eff={rc.risk_efficiency:.2f}  "
            f"trades={rc.metrics.total_trades}  "
            f"mask=0x{rc.mask:014x}\n    {conditions}"
        )
    return "\n".join(lines)


__all__ = [
    "ExplainedCombo",
    "explain_combos",
    "render_leaderboard",
]

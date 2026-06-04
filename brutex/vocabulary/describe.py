"""Human-readable decode of a strategy mask — which conditions matched, by family.

A strategy combo is a u64 mask; each set bit ``0..51`` is one atomic condition from
the 52-vocabulary (:mod:`brutex.vocabulary.bits`). This module turns a winning mask
back into the EXACT list of matched conditions, grouped by the 9 indicator families,
so a leaderboard combo reads as a rule ("close_above_ema20 AND near_fib_618 AND
bar_bullish") rather than an opaque number — the explainability the brute-force
search exists to deliver (CLAUDE.md §1: no black box, every winning combo traces to
its matched conditions).

Decode only — no bar evaluation, no I/O. The reserved bits ``52..63`` (headroom per
``bits.py``) are ignored, so a forward-compatible mask never raises here.

O(1) per mask: a bounded scan of the 52 condition bits (independent of bar count).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.vocabulary.bits import BIT_TO_NAME, TOTAL_CONDITIONS

#: The 9 indicator families and their contiguous bit ranges — the canonical grouping
#: documented in ``bits.py`` (Group 1..9). Kept as the ONE place the family→bit-range
#: correspondence lives (CLAUDE.md §11 COMMON); validated against the 52-bit vocabulary
#: at import so a future bit change cannot silently desync the grouping.
GROUP_RANGES: Final[tuple[tuple[str, range], ...]] = (
    ("EMA Position", range(0, 6)),
    ("Pivot Proximity", range(6, 13)),
    ("PDH/PDL", range(13, 19)),
    ("Fibonacci", range(19, 30)),
    ("Candle Shape", range(30, 37)),
    ("Prior Bar Pattern", range(37, 40)),
    ("Day Structure", range(40, 44)),
    ("Time of Day", range(44, 48)),
    ("Day Type", range(48, 52)),
)


@dataclass(frozen=True, slots=True)
class MaskGroup:
    """One indicator family with the conditions a mask matched within it.

    Attributes
    ----------
    group
        The strategy-family name (e.g. ``"Fibonacci"``).
    conditions
        The matched condition names in this family, in ascending bit order.
    """

    group: str
    conditions: tuple[str, ...]


def _check_group_invariants(groups: tuple[tuple[str, range], ...], total: int) -> None:
    """Validate that ``groups`` partitions the ``0..total-1`` condition bits exactly.

    Extracted so the ``raise`` branch is reachable from tests (call with a tampered
    grouping). Because ``bits.py`` already guarantees ``BIT_TO_NAME`` spans exactly
    ``0..total-1``, a clean partition here also guarantees every grouped bit has a
    name — so no separate name check is needed.

    Raises
    ------
    AssertionError
        If the group ranges do not cover ``0..total-1`` exactly once (gap/overlap).
    """
    covered = [bit for _, rng in groups for bit in rng]
    if sorted(covered) != list(range(total)):
        raise AssertionError(
            f"GROUP_RANGES must partition bits 0..{total - 1} exactly once, got {sorted(covered)}"
        )


# Compile-time invariant — the grouping must cover the whole 52-bit vocabulary.
_check_group_invariants(GROUP_RANGES, TOTAL_CONDITIONS)


def matched_condition_names(mask: int) -> list[str]:
    """Return the condition names a mask requires, in ascending bit order.

    Reserved bits ``52..63`` are ignored, so a forward-compatible mask is safe.

    Parameters
    ----------
    mask
        The u64 strategy mask.

    Returns
    -------
    list[str]
        The matched condition names (empty for the no-condition mask ``0``).
    """
    return [BIT_TO_NAME[bit] for bit in range(TOTAL_CONDITIONS) if (mask >> bit) & 1]


def describe_mask(mask: int) -> list[MaskGroup]:
    """Decode a strategy mask into its matched conditions, grouped by family.

    Only families with at least one matched condition are returned, in canonical
    family order — so the result reads as the combo's rule across its strategy
    families. Reserved bits ``52..63`` are ignored.

    Parameters
    ----------
    mask
        The u64 strategy mask (e.g. a leaderboard combo).

    Returns
    -------
    list[MaskGroup]
        One :class:`MaskGroup` per family that has a matched condition (empty list
        for the no-condition mask ``0``).
    """
    result: list[MaskGroup] = []
    for group, rng in GROUP_RANGES:
        names = tuple(BIT_TO_NAME[bit] for bit in rng if (mask >> bit) & 1)
        if names:
            result.append(MaskGroup(group=group, conditions=names))
    return result


__all__ = [
    "GROUP_RANGES",
    "MaskGroup",
    "describe_mask",
    "matched_condition_names",
]

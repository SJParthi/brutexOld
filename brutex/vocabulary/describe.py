"""Human-readable decode of a strategy mask — which conditions matched, by family.

A strategy combo is a u128 mask; each set bit ``0..TOTAL_CONDITIONS-1`` is one atomic
condition from the shared vocabulary (:mod:`brutex.vocabulary.bits`). This module turns
a winning mask back into the EXACT list of matched conditions, grouped by indicator
family, so a leaderboard combo reads as a rule ("close_above_ema20 AND near_fib_618 AND
bar_bullish") rather than an opaque number — the explainability the brute-force search
exists to deliver (CLAUDE.md §1: no black box, every winning combo traces to its
matched conditions).

Decode only — no bar evaluation, no I/O. Bits ``>= TOTAL_CONDITIONS`` (reserved
headroom per ``bits.py``) are ignored, so a forward-compatible mask never raises here.

O(1) per mask: a bounded scan of the condition bits (independent of bar count). The
family→bit-range table (:data:`GROUP_RANGES`) is re-exported from the generated
:mod:`brutex.vocabulary.bits` — the codegen owns it (sourced from ``conditions.toml``),
so it is the ONE place the grouping lives (CLAUDE.md §11 COMMON /
DEC-VOCAB-U128-CODEGEN-001).
"""

from __future__ import annotations

from dataclasses import dataclass

from brutex.vocabulary.bits import BIT_TO_NAME, GROUP_RANGES, TOTAL_CONDITIONS


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


# Compile-time invariant — the grouping must cover the whole vocabulary exactly.
_check_group_invariants(GROUP_RANGES, TOTAL_CONDITIONS)


def matched_condition_names(mask: int) -> list[str]:
    """Return the condition names a mask requires, in ascending bit order.

    Bits ``>= TOTAL_CONDITIONS`` are ignored, so a forward-compatible mask is safe.

    Parameters
    ----------
    mask
        The u128 strategy mask.

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
    families. Bits ``>= TOTAL_CONDITIONS`` are ignored.

    Parameters
    ----------
    mask
        The u128 strategy mask (e.g. a leaderboard combo).

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

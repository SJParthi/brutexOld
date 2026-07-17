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

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from brutex.vocabulary.bits import (
    BIT_TO_NAME,
    GROUP_RANGES,
    NAME_TO_BIT,
    RETIRED_MASK,
    TOTAL_CONDITIONS,
)

#: Display suffix for a RETIRED (tombstoned) condition bit — a bit whose math
#: was deliberately deleted (``reserved = true`` in ``conditions.toml``; e.g.
#: the VWAP bits 52/53 after the spot-only volume wipe). The evaluator emits 0
#: for these forever, so a PERSISTED pre-reshape mask that carries one can
#: never fire again — rendering the plain name would present a dead condition
#: as live (TOMB-01). ONE suffix authority (CLAUDE.md §11 COMMON): every
#: renderer goes through :func:`condition_display_name` /
#: :func:`display_condition_names`, never a copied literal.
RETIRED_SUFFIX: Final[str] = " (retired)"


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


def condition_display_name(bit: int) -> str:
    """Return the display name of condition *bit* — tombstone-aware (TOMB-01).

    The ONE rendering authority for a condition bit's human-readable name: a
    RETIRED bit (:data:`~brutex.vocabulary.bits.RETIRED_MASK` — ``reserved =
    true`` tombstones whose math was deleted, e.g. the VWAP bits 52/53) reads
    ``"<name> (retired)"`` so a pre-reshape persisted mask never presents a
    dead condition as live; every other declared bit — implemented AND
    LAYOUT-ONLY (declared-but-unimplemented, which no persisted mask can
    carry: :class:`~brutex.strategies.spec.StrategySpec` refuses them; none
    exist on today's layout — the SuperTrend bits 64/65 left that class at
    the vocab-66 implemented-flip, crate 0.5.0) — reads its plain name.

    Parameters
    ----------
    bit
        A declared condition bit (``0 <= bit < TOTAL_CONDITIONS``).

    Returns
    -------
    str
        The display name (suffixed for tombstoned bits only).

    Raises
    ------
    KeyError
        If *bit* is not a declared condition bit.
    """
    name = BIT_TO_NAME[bit]
    if (RETIRED_MASK >> bit) & 1:
        return f"{name}{RETIRED_SUFFIX}"
    return name


def display_condition_names(names: Iterable[str]) -> list[str]:
    """Map RAW condition names (e.g. a persisted artifact's) to display names.

    The read-side twin of :func:`condition_display_name` for surfaces that
    persisted plain condition-NAME strings before the tombstone reshape (the
    one-click ``top100.json`` ``conditions`` arrays): a plain name that maps
    to a RETIRED bit gains the ``" (retired)"`` suffix; every other entry —
    live names, already-suffixed names, and unknown/junk strings alike —
    passes through VERBATIM (an old payload must never crash or be dropped
    here; idempotent by construction).

    Parameters
    ----------
    names
        Raw condition-name strings (possibly from an old persisted payload).

    Returns
    -------
    list[str]
        The display names, in input order.
    """
    out: list[str] = []
    for name in names:
        bit = NAME_TO_BIT.get(name)
        out.append(name if bit is None else condition_display_name(bit))
    return out


def matched_condition_names(mask: int) -> list[str]:
    """Return the condition DISPLAY names a mask requires, in ascending bit order.

    Bits ``>= TOTAL_CONDITIONS`` are ignored, so a forward-compatible mask is safe.
    Tombstone-aware (TOMB-01): a RETIRED bit renders through
    :func:`condition_display_name` (``"<name> (retired)"``) — a pre-reshape
    persisted mask carrying the dead VWAP bits reads honestly, never as a live
    condition. New sweeps never mint retired bits (the default grid derives
    from ``IMPLEMENTED_MASK`` and :class:`~brutex.strategies.spec.StrategySpec`
    refuses them), so current-era output is byte-identical.

    Parameters
    ----------
    mask
        The u128 strategy mask.

    Returns
    -------
    list[str]
        The matched condition display names (empty for the no-condition mask ``0``).
    """
    return [condition_display_name(bit) for bit in range(TOTAL_CONDITIONS) if (mask >> bit) & 1]


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
        names = tuple(condition_display_name(bit) for bit in rng if (mask >> bit) & 1)
        if names:
            result.append(MaskGroup(group=group, conditions=names))
    return result


__all__ = [
    "GROUP_RANGES",
    "RETIRED_SUFFIX",
    "MaskGroup",
    "condition_display_name",
    "describe_mask",
    "display_condition_names",
    "matched_condition_names",
]

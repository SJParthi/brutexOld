"""The pure cross-phase verdict threaded INTO the historical phase — a LEAF.

DEC-PULL-SIX-STEP-ORDER-001. :class:`PriorStepsVerdict` is COMPUTED by the
pipeline sequencer (:mod:`brutex.dashboard.pipeline_pull`) and CONSUMED at the
historical tail (:mod:`brutex.dashboard.composite_pull` →
:mod:`brutex.dashboard.historical_pull`), which the sequencer itself imports at
module level. Housing the dataclass in the sequencer therefore made EVERY
module-level edge of that chain cyclic (the consumers needed a
``pipeline_pull`` import back up the chain — even ``TYPE_CHECKING``-only, which
CodeQL's module-level cyclic-import analysis still counts). This module is the
dependency-free LEAF home both sides import without a cycle: it imports
NOTHING from :mod:`brutex.dashboard` (keep it that way). The sequencer
re-exports the name, so ``from brutex.dashboard.pipeline_pull import
PriorStepsVerdict`` keeps working (CLAUDE.md §11 COMMON — one definition, one
back-compat alias, no copy).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriorStepsVerdict:
    """Spot + metadata cleanliness threaded INTO the historical phase.

    DEC-PULL-SIX-STEP-ORDER-001: the Greeks seam lives at the historical
    tail (:mod:`brutex.dashboard.historical_pull`), which alone cannot see a
    SPOT-side quarantine — under the strict barrier the sequencer computes
    this verdict AFTER the spot + metadata gates pass and threads it
    ``run_historical`` → composite → ``pull_contract_candles`` so the seam
    can withhold the enrichment on ANY prior-step uncleanliness. A
    non-pipeline caller passes ``None`` (today's behavior, byte-identical).

    Attributes
    ----------
    spot_clean
        The spot phase gate passed AND zero spot symbols were quarantined.
    metadata_clean
        The metadata phase gate passed.
    strict
        The resolved :func:`~brutex.dashboard.pipeline_pull.strict_gate_enabled`
        flag — ``False`` keeps the Greeks seam's cleanset behavior
        byte-identical.
    """

    spot_clean: bool
    metadata_clean: bool
    strict: bool

    @property
    def prior_clean(self) -> bool:
        """``True`` iff every step BEFORE the F&O phase settled clean."""
        return self.spot_clean and self.metadata_clean

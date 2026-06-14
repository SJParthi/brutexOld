"""Derivative interval resolution — the surviving knob of the retired bulk chain pull.

The fused bulk "Pull expiries + contracts" engine (``pull_all_derivatives``) that
once enumerated the WHOLE F&O chain AND downloaded every contract's full-range
candles in one click was DECOMMISSIONED in PR-6 of the metadata/historical split
(DEC-DERIV-DECOMMISSION-FUSED-001, ADR-0010). One click no longer triggers the
runaway "download ALL candles" behaviour: the metadata HALF moved to the
metadata-only :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`
("⛓ Pull expiries + contracts" → ``/pull-expiries-contracts``) and the candle HALF
moved to the selection-based
:func:`~brutex.dashboard.historical_pull.pull_contract_candles`
("⤓ Pull historical" → ``/pull-historical``).

What remains here is the small, still-used interval knob
(:func:`resolve_pull_intervals`) — the "Pull historical" route maps its
``1day`` / ``1minute`` / ``both`` choice to the ordered intervals to pull through
this single source of truth (CLAUDE.md §11 COMMON), so the ``both`` ordering
(``1day`` BEFORE ``1minute`` — the cheap, NSE-verifiable skeleton lands before the
heavy minute fill) lives in exactly one place.
"""

from __future__ import annotations

from typing import Final

#: The ordered intervals for a ``both`` pull — ``1day`` BEFORE ``1minute`` so the
#: cheap, NSE-verifiable skeleton lands before the heavy minute fill (operator
#: decree, DEC-PULL-DERIVATIVES-001).
GROWW_DERIVATIVE_INTERVALS_ORDER: Final[tuple[str, str]] = ("1day", "1minute")


def resolve_pull_intervals(choice: str) -> tuple[str, ...]:
    """Map an interval *choice* to the ordered intervals to pull.

    ``"1day"`` → ``("1day",)``; ``"1minute"`` → ``("1minute",)``; ``"both"`` →
    :data:`GROWW_DERIVATIVE_INTERVALS_ORDER` (``1day`` first). An unknown choice
    raises ``ValueError`` (surfaced as a 400 by the route).
    """
    if choice == "1day":
        return ("1day",)
    if choice == "1minute":
        return ("1minute",)
    if choice == "both":
        return GROWW_DERIVATIVE_INTERVALS_ORDER
    raise ValueError(f"interval must be one of '1day' / '1minute' / 'both', got {choice!r}")

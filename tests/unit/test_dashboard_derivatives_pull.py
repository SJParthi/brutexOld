# pyright: reportPrivateUsage=none
"""Tests for ``brutex.dashboard.derivatives_pull`` (DEC-PULL-DERIVATIVES-001).

The fused bulk derivatives pull (``pull_all_derivatives`` + its helpers) was
DECOMMISSIONED in PR-6 of the metadata/historical split
(DEC-DERIV-DECOMMISSION-FUSED-001, ADR-0010): one click no longer downloads the
whole chain's candles. The metadata HALF moved to
``brutex.dashboard.metadata_pull`` and the candle HALF to
``brutex.dashboard.historical_pull`` (each fully covered by its own test module).
All that survives this module is the small interval knob
:func:`~brutex.dashboard.derivatives_pull.resolve_pull_intervals` — the
"Pull historical" route's single source of truth for the ``1day`` / ``1minute`` /
``both`` ordering. 100% line+branch on ``brutex.dashboard.derivatives_pull``.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.derivatives_pull import (
    GROWW_DERIVATIVE_INTERVALS_ORDER,
    resolve_pull_intervals,
)


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("1day", ("1day",)), ("1minute", ("1minute",)), ("both", GROWW_DERIVATIVE_INTERVALS_ORDER)],
)
def test_resolve_pull_intervals(choice: str, expected: tuple[str, ...]) -> None:
    assert resolve_pull_intervals(choice) == expected


def test_resolve_pull_intervals_both_is_one_day_first() -> None:
    # The whole point: 1day BEFORE 1minute.
    assert resolve_pull_intervals("both") == ("1day", "1minute")


def test_resolve_pull_intervals_bad() -> None:
    with pytest.raises(ValueError, match="interval must be one of"):
        resolve_pull_intervals("5minute")
